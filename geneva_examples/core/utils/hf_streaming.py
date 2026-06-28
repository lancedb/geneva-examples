"""Streaming, bounded, credential-re-vending Hugging Face -> Geneva ingest.

A one-shot ``tbl.add(staged)`` of a multi-TB dataset on a ``db://`` enterprise
connection dies after ~1h with S3 ``ExpiredToken``: the table is opened once, the
vended STS credentials are baked into the Lance backend, and the single long
write outlives the token TTL. The client-only mitigation has two independent
levers, both implemented here:

* **Bounded sub-writes** -- :func:`iter_hf_batches` streams the source into
  ``chunk_rows``-sized :class:`pyarrow.RecordBatch` es so each ``tbl.add(...)``
  finishes well inside the STS TTL.
* **Re-vend per chunk** -- :func:`fresh_table` returns a table handle with
  freshly vended credentials before each chunk. The default ``connect`` mode
  reconstructs the connection (the only lever guaranteed to re-vend); ``reopen``
  and ``latest`` are lighter alternatives (see the docstring).

The network-bound source readers carry ``# pragma: no cover`` -- the bounded
iterator and the re-vend lever are pure and unit-tested.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pyarrow as pa

if TYPE_CHECKING:
    from geneva_examples.core.config import Config

logger = logging.getLogger(__name__)

SOURCE_MODES = ("datasets", "parquet")
REVEND_MODES = ("connect", "reopen", "latest")


def iter_hf_batches(
    hf_dataset: str,
    hf_split: str,
    chunk_rows: int,
    limit: int | None = None,
    skip_rows: int = 0,
    mode: str = "datasets",
    hf_token: str | None = None,
) -> Iterator[pa.RecordBatch]:
    """Yield bounded ``RecordBatch`` es from a Hugging Face dataset.

    ``skip_rows`` source rows are dropped first (resume), then rows are yielded
    until ``limit`` *source* rows have been consumed (``skip_rows`` counts toward
    ``limit``, so resuming a ``--limit N`` load tops the table up to ``N`` total).
    Both bounds slice within the boundary batch, so the row counts are exact
    regardless of the source's native batch size.
    """
    if mode not in SOURCE_MODES:
        raise ValueError(
            f"unknown source mode: {mode!r} (expected one of {SOURCE_MODES})"
        )

    skipped = 0
    yielded = 0
    for batch in _raw_batches(mode, hf_dataset, hf_split, chunk_rows, hf_token):
        # Drop the resume prefix, slicing within the batch that straddles it.
        if skipped < skip_rows:
            need = skip_rows - skipped
            if batch.num_rows <= need:
                skipped += batch.num_rows
                continue
            batch = batch.slice(need)
            skipped = skip_rows

        # Cap at `limit` source rows (skipped + yielded), slicing the boundary.
        if limit is not None:
            consumed = skipped + yielded
            if consumed >= limit:
                return
            remaining = limit - consumed
            if batch.num_rows > remaining:
                batch = batch.slice(0, remaining)

        if batch.num_rows == 0:
            continue

        yield batch
        yielded += batch.num_rows
        if limit is not None and skipped + yielded >= limit:
            return


def _raw_batches(
    mode: str,
    hf_dataset: str,
    hf_split: str,
    chunk_rows: int,
    hf_token: str | None,
) -> Iterator[pa.RecordBatch]:  # pragma: no cover - network-bound source readers
    """Dispatch to the per-mode raw batch reader (no bounding applied)."""
    if mode == "parquet":
        return _parquet_batches(hf_dataset, chunk_rows, hf_token)
    return _datasets_batches(hf_dataset, hf_split, chunk_rows, hf_token)


def _parquet_batches(
    hf_dataset: str,
    chunk_rows: int,
    hf_token: str | None,
) -> Iterator[pa.RecordBatch]:  # pragma: no cover - streams parquet from hf://
    """Stream parquet metadata straight from ``hf://`` -- no full download.

    Scales to datacomp-style metadata repos (e.g. ``mlfoundations/datacomp_xlarge``):
    point :mod:`pyarrow.dataset` at the repo's parquet files over
    :class:`huggingface_hub.HfFileSystem` and read fixed-size batches. Set
    ``HF_TOKEN`` (``hf_token``) for gated repos.

    Reads *every* ``*.parquet`` under the repo as one dataset (the intended
    behavior for sharded, single-split metadata repos); ``hf_split`` is not
    applied here -- use ``datasets`` mode for per-split selection.
    """
    import pyarrow.dataset as ds
    from huggingface_hub import HfFileSystem

    fs = HfFileSystem(token=hf_token)
    root = f"datasets/{hf_dataset}"
    files = fs.glob(f"{root}/**/*.parquet")
    if not files:
        raise RuntimeError(f"no parquet files under hf://{root}")
    dataset = ds.dataset(files, filesystem=fs, format="parquet")
    yield from dataset.to_batches(batch_size=chunk_rows)


def _datasets_batches(
    hf_dataset: str,
    hf_split: str,
    chunk_rows: int,
    hf_token: str | None,
) -> Iterator[pa.RecordBatch]:  # pragma: no cover - streams via `datasets`
    """Stream a general dataset via ``datasets`` and buffer rows into batches.

    The first batch's schema is reused for every later batch so all ``add``
    calls are schema-consistent. Columns must be Arrow-serializable scalars
    (str/int/float/bool/list/dict) -- e.g. parquet metadata or text; decode-heavy
    feature types (PIL images, audio) are out of scope for this example.
    """
    from datasets import load_dataset

    stream = load_dataset(hf_dataset, split=hf_split, streaming=True, token=hf_token)
    buffer: list[dict] = []
    schema: pa.Schema | None = None

    def _flush(rows: list[dict]) -> pa.RecordBatch:
        nonlocal schema
        batch = (
            pa.RecordBatch.from_pylist(rows)
            if schema is None
            else pa.RecordBatch.from_pylist(rows, schema=schema)
        )
        schema = batch.schema
        return batch

    for row in stream:
        buffer.append(dict(row))
        if len(buffer) >= chunk_rows:
            yield _flush(buffer)
            buffer = []
    if buffer:
        yield _flush(buffer)


def fresh_table(
    cfg: Config,
    table_name: str,
    *,
    mode: str = "connect",
    conn: object | None = None,
    table: object | None = None,
) -> tuple[object, object]:
    """Return ``(conn, table)`` with freshly vended credentials for the next add.

    * ``connect`` (default) -- reconstruct the connection via
      :func:`geneva_examples.core.common.connect`, then ``open_table``. This is
      the only lever guaranteed to re-vend by construction (fresh namespace
      client -> fresh ``describe_table`` -> fresh STS creds).
    * ``reopen`` -- reuse ``conn`` and ``open_table`` again. Lighter, but whether
      it re-vends happens inside opaque Rust and is unconfirmed; reuse with care.
    * ``latest`` -- hold one ``table`` and refresh its credentials in place via
      the underlying ``latest_storage_options()`` vend primitive (a private,
      internal lancedb API).
    """
    if mode == "connect":
        from geneva_examples.core.common import connect

        conn = connect(cfg)
        return conn, conn.open_table(table_name)

    if mode == "reopen":
        if conn is None:
            raise ValueError("reopen mode requires an existing conn")
        return conn, conn.open_table(table_name)

    if mode == "latest":
        if conn is None:
            raise ValueError("latest mode requires an existing conn")
        if table is None:
            table = conn.open_table(table_name)
        try:
            table._ltbl.latest_storage_options()
        except Exception:  # noqa: BLE001 - best-effort refresh; add() still proceeds
            logger.warning("latest_storage_options_unavailable")
        return conn, table

    raise ValueError(f"unknown revend mode: {mode!r} (expected one of {REVEND_MODES})")


def vended_token_prefix(table: object, length: int = 8) -> str:
    """Best-effort short prefix of the table's vended ``aws_session_token``.

    Logged per chunk so a ``db://`` run self-verifies that credentials actually
    rotate across chunks. Returns ``"<none>"`` when no token is available.
    """
    try:
        opts = table._ltbl.latest_storage_options() or {}  # type: ignore[attr-defined]
        token = opts.get("aws_session_token") or ""
        return token[:length] if token else "<none>"
    except Exception:  # noqa: BLE001 - logging helper must never raise
        return "<none>"
