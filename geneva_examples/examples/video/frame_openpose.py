"""Frame OpenPose CLI: pose-skeleton PNGs on video_clips.frame (GPU)."""

from __future__ import annotations

import logging
import os

from geneva_examples.core.backfill import backfill_column
from geneva_examples.core.common import (
    build_manifest,
    connect,
    local_or,
    resolve_resources,
    runtime_session,
)
from geneva_examples.core.config import Config

logger = logging.getLogger(__name__)


def run(
    cfg: Config,
    *,
    table_name: str = "video_clips",
    input_column: str = "frame",
    output_column: str = "pose",
    include_hand: bool = True,
    include_face: bool = True,
    batch_size: int = 256,
    num_workers: int = 4,
    num_cpus: float = 2.0,
    num_gpus: float | None = None,
    memory_gib: int = 1,
    checkpoint_size: int = 4096,
    task_size: int = 4096,
    concurrency: int = 32,
    backfill_timeout_min: int = 1000,
    flush_interval_s: float = 30.0,
    schema_wait_attempts: int = 30,
    schema_wait_sleep_s: int = 2,
) -> None:
    """Add an OpenPose skeleton (PNG bytes) column to the frames table."""
    os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")

    import geneva

    from geneva_examples.examples.video.openpose import (
        OPENPOSE_RUNTIME_PIP,
        build_openpose_udf,
    )

    resolved_gpus = num_gpus if num_gpus is not None else 0.25
    num_cpus, resolved_gpus, memory_bytes = resolve_resources(
        cfg, num_cpus=num_cpus, num_gpus=resolved_gpus, memory_gib=memory_gib
    )
    batch_size = local_or(cfg, 8, batch_size)
    num_workers = local_or(cfg, 0, num_workers)
    concurrency = local_or(cfg, 1, concurrency)

    logger.info("geneva_version %s mode %s", geneva.__version__, cfg.mode)
    logger.info("db_uri %s table %s column %s", cfg.db_uri, table_name, input_column)
    logger.info(
        "batch_size %s num_workers %s num_gpus %s",
        batch_size,
        num_workers,
        resolved_gpus,
    )

    conn = connect(cfg)
    table = conn.open_table(table_name)

    manifest = build_manifest(cfg, "frame-openpose", OPENPOSE_RUNTIME_PIP)
    udf = build_openpose_udf(
        input_column=input_column,
        manifest=manifest,
        batch_size=batch_size,
        num_workers=num_workers,
        num_cpus=num_cpus,
        num_gpus=resolved_gpus,
        memory_bytes=memory_bytes,
        checkpoint_size=checkpoint_size,
        task_size=task_size,
        include_hand=include_hand,
        include_face=include_face,
    )
    with runtime_session(conn, cfg):
        backfill_column(
            conn=conn,
            table=table,
            table_name=table_name,
            column=output_column,
            udf=udf,
            concurrency=concurrency,
            task_size=task_size,
            checkpoint_size=checkpoint_size,
            flush_interval_s=flush_interval_s,
            timeout_min=backfill_timeout_min,
            wait_attempts=schema_wait_attempts,
            wait_sleep_s=schema_wait_sleep_s,
        )
    logger.info("frame_openpose_ok")
