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

from geneva_examples.core.common import connect, format_sample
from geneva_examples.core.config import Config
from geneva_examples.core.utils.retry import retry_io

logger = logging.getLogger(__name__)


def run(
    cfg: Config,
    *,
    table_name: str = "videos",
    limit: int = 256,
    batch_size: int = 256,
    openvid_uri: str = "hf://datasets/lance-format/openvid-lance/data",
    openvid_table: str = "train",
    skip_null_video: bool = True,
    overwrite: bool = False,
    table_write_retries: int = 5,
    table_write_retry_sleep_s: float = 2.0,
) -> None:
    """Stream OpenVid rows into the configured videos table."""
    os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")
    os.environ.setdefault("HF_HOME", "./huggingface_cache")

    import geneva
    import lance

    from geneva_examples.core.utils.videos import (
        OPENVID_SOURCE_COLUMNS,
        normalize_openvid_reference_batch,
    )

    # Reference-only ingest: scan with the *default* blob handling so `video_blob`
    # stays a descriptor (struct<position,size> — no bytes pulled to the client),
    # and request `with_row_id=True` so each row's stable `_rowid` is captured as
    # the `openvid_rowid` pointer. The chunker uses that pointer later to read the
    # blob directly on the cluster.
    dataset_uri = f"{openvid_uri.rstrip('/')}/{openvid_table}.lance"

    logger.info("geneva_version %s mode %s", geneva.__version__, cfg.mode)
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
        "initial_sample\n%s",
        format_sample(
            table.search().select(["video_id", "openvid_rowid"]).limit(5).to_list()
        ),
    )
    logger.info("ingest_videos_openvid_ok")
