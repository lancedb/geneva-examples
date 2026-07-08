"""Load local PDF files into PyArrow record batches for ingest."""

from __future__ import annotations

import logging
from pathlib import Path

import pyarrow as pa

logger = logging.getLogger(__name__)


def load_pdf_batches(
    pdf_dir: str | Path,
    frag_size: int = 1,
) -> list[pa.RecordBatch]:
    """Read every ``*.pdf`` under ``pdf_dir`` into ``doc_id`` + ``pdf_bytes`` batches.

    Each file's stem becomes ``doc_id`` and its raw bytes ``pdf_bytes`` — the
    column name the ``extract_pages`` UDF binds to. ``frag_size`` rows per batch;
    the default of 1 puts each PDF in its own fragment so the remote extract/chunk
    backfill can process them in parallel. Files are visited in sorted order and
    duplicate stems get a ``-N`` suffix so ``doc_id`` stays unique.
    """
    folder = Path(pdf_dir).expanduser()
    if not folder.is_dir():
        raise FileNotFoundError(f"no PDF directory at {folder}")

    paths = sorted(
        p
        for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() == ".pdf" and not p.name.startswith(".")
    )
    if not paths:
        raise FileNotFoundError(f"no .pdf files in {folder}")

    batches: list[pa.RecordBatch] = []
    batch: list[dict] = []
    seen: dict[str, int] = {}
    for path in paths:
        stem = path.stem
        count = seen.get(stem, 0)
        seen[stem] = count + 1
        doc_id = stem if count == 0 else f"{stem}-{count}"
        logger.info(
            "reading %s -> doc_id %s (%d bytes)", path.name, doc_id, path.stat().st_size
        )
        batch.append({"doc_id": doc_id, "pdf_bytes": path.read_bytes()})
        if len(batch) >= frag_size:
            batches.append(_to_batch(batch))
            batch = []
    if batch:
        batches.append(_to_batch(batch))
    return batches


def _to_batch(rows: list[dict]) -> pa.RecordBatch:
    schema = pa.schema(
        [
            pa.field("doc_id", pa.string()),
            pa.field("pdf_bytes", pa.large_binary()),
        ]
    )
    return pa.RecordBatch.from_pylist(rows, schema=schema)
