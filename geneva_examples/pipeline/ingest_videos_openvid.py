"""OpenVid ingest CLI: register the lance-format OpenVid dataset into LanceDB.

Reads the first ``--limit`` rows (scan order) of the OpenVid lance dataset
(``hf://datasets/lance-format/openvid-lance``) and writes a *reference-only*
``videos`` table: the raw MP4 bytes are **not** ingested. The OpenVid
``video_path`` becomes ``video_id`` and the source row's ``_rowid`` is captured
as ``openvid_rowid`` — a pointer the chunker uses to read the blob directly on
the cluster (via ``take_blobs``). All other columns (caption, embedding,
aesthetic/motion/temporal scores, camera_motion, fps, seconds, frame) ride along
as metadata, joinable on ``video_id``.

Because no video bytes flow through the client, ingest is cheap and fast; the
heavy byte movement happens cluster-side during chunking. Rows still stream in
small batches, each written incrementally with retries.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import typer

from geneva_examples.core.common import connect, setup_logging
from geneva_examples.core.config import load_config
from geneva_examples.core.utils.retry import retry_io

logger = logging.getLogger(__name__)

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def run(
    config: Path | None = typer.Option(None, "--config", help="Path to config.yaml."),
    log_level: str = typer.Option("INFO", help="Logging level."),
    db_uri: str | None = typer.Option(None, help="Override config db_uri."),
    table_name: str = typer.Option("videos", help="Target table name."),
    limit: int = typer.Option(
        256, help="Max OpenVid rows to ingest (first N in scan order)."
    ),
    batch_size: int = typer.Option(
        256, help="Rows per streamed record batch (bounds client memory)."
    ),
    openvid_uri: str = typer.Option(
        "hf://datasets/lance-format/openvid-lance/data",
        help="Base URI holding the OpenVid lance dataset (a '<table>.lance' dir).",
    ),
    openvid_table: str = typer.Option(
        "train", help="OpenVid dataset name (resolves to <uri>/<table>.lance)."
    ),
    skip_null_video: bool = typer.Option(
        True, help="Drop rows whose video bytes are null."
    ),
    overwrite: bool = typer.Option(
        False, help="Drop the table first if it already exists."
    ),
    table_write_retries: int = typer.Option(5, help="Retries for create/add ops."),
    table_write_retry_sleep_s: float = typer.Option(
        2.0, help="Base sleep (seconds) between table-write retries."
    ),
) -> None:
    """Stream OpenVid rows into the configured videos table."""
    setup_logging(log_level)
    os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")
    os.environ.setdefault("HF_HOME", "./huggingface_cache")

    import geneva
    import lance

    from geneva_examples.core.utils.videos import (
        OPENVID_SOURCE_COLUMNS,
        normalize_openvid_reference_batch,
    )

    cfg = load_config(config)
    if db_uri:
        cfg.db_uri = db_uri

    # Reference-only ingest: scan with the *default* blob handling so `video_blob`
    # stays a descriptor (struct<position,size> — no bytes pulled to the client),
    # and request `with_row_id=True` so each row's stable `_rowid` is captured as
    # the `openvid_rowid` pointer. The chunker uses that pointer later to read the
    # blob directly on the cluster.
    dataset_uri = f"{openvid_uri.rstrip('/')}/{openvid_table}.lance"

    logger.info("geneva_version %s", geneva.__version__)
    logger.info(
        "db_uri %s table %s source %s limit %s",
        cfg.db_uri,
        table_name,
        dataset_uri,
        limit,
    )

    conn = connect(cfg)

    src = lance.dataset(dataset_uri)
    reader = src.scanner(
        columns=OPENVID_SOURCE_COLUMNS,
        limit=limit,
        batch_size=batch_size,
        with_row_id=True,
    ).to_batches()

    if overwrite:
        try:
            conn.drop_table(table_name)
            logger.info("dropped_existing_table %s", table_name)
        except Exception:  # noqa: BLE001
            pass

    table = None
    rows_written = 0
    next_log = 500
    for batch_index, raw in enumerate(reader, start=1):
        norm = normalize_openvid_reference_batch(raw, skip_null_video=skip_null_video)
        if norm.num_rows == 0:
            continue
        if table is None:
            table = retry_io(
                "create_table",
                lambda b=norm: conn.create_table(table_name, data=b),
                attempts=table_write_retries,
                sleep_s=table_write_retry_sleep_s,
            )
        else:
            retry_io(
                f"add_batch_{batch_index}",
                lambda b=norm, t=table: t.add(b),
                attempts=table_write_retries,
                sleep_s=table_write_retry_sleep_s,
            )
        rows_written += norm.num_rows
        if rows_written >= next_log:
            logger.info("rows_written %d (target %d)", rows_written, limit)
            next_log += 500
        if rows_written >= limit:
            break

    if table is None:
        raise RuntimeError(
            "no rows ingested from OpenVid (empty result or every video_blob was null)"
        )

    logger.info("rows_created %s", table.count_rows())
    logger.info("table_columns %s", table.schema.names)
    try:
        logger.info("table_names %s", conn.table_names())
    except Exception:  # noqa: BLE001
        pass
    logger.info(
        "initial_sample %s",
        table.search().select(["video_id", "openvid_rowid"]).limit(5).to_list(),
    )
    logger.info("ingest_videos_openvid_ok")


if __name__ == "__main__":
    app()
