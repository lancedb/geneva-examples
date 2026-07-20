"""Frame embedding step: OpenCLIP embeddings on video_clips.frame (GPU/CPU)."""

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
    output_column: str = "embedding",
    model_name: str = "ViT-B-32",
    pretrained: str = "laion2b_s34b_b79k",
    dim: int = 512,
    batch_size: int = 256,
    num_workers: int = 12,
    num_cpus: float = 4.0,
    num_gpus: float | None = None,
    memory_gib: int = 1,
    checkpoint_size: int = 65536,
    task_size: int = 65536,
    concurrency: int = 16,
    backfill_timeout_min: int = 1000,
    flush_interval_s: float = 30.0,
    schema_wait_attempts: int = 30,
    schema_wait_sleep_s: int = 2,
    reset: bool = False,
) -> None:
    """Add an OpenCLIP embedding column to the frames table.

    By default the backfill is **incremental**: it only embeds clips whose
    ``embedding`` is still null, so a partial/failed run can be re-run cheaply
    and each pass picks up whatever clips landed since the last one. Run it only
    once the chunk job has **finished** — adding the ``embedding`` column is a
    schema change that breaks a still-running chunker's schema-matched appends
    (see :func:`geneva_examples.core.backfill.backfill_column`). Pass
    ``reset=True`` (``--reset``) to drop the column and recompute every row —
    e.g. after switching ``--model-name``.
    """
    os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")

    import geneva

    from geneva_examples.examples._shared.clip import (
        CLIP_RUNTIME_PIP,
        build_clip_embedding_udf,
    )

    resolved_gpus = num_gpus if num_gpus is not None else 0.5
    num_cpus, resolved_gpus, memory_bytes = resolve_resources(
        cfg, num_cpus=num_cpus, num_gpus=resolved_gpus, memory_gib=memory_gib
    )
    batch_size = local_or(cfg, 8, batch_size)
    num_workers = local_or(cfg, 0, num_workers)
    concurrency = local_or(cfg, 1, concurrency)

    logger.info("geneva_version %s mode %s", geneva.__version__, cfg.mode)
    logger.info("db_uri %s table %s column %s", cfg.db_uri, table_name, input_column)
    logger.info(
        "model %s pretrained %s dim %s batch_size %s num_workers %s num_gpus %s",
        model_name,
        pretrained,
        dim,
        batch_size,
        num_workers,
        resolved_gpus,
    )

    conn = connect(cfg)
    table = conn.open_table(table_name)

    manifest = build_manifest(cfg, "frame-embed", CLIP_RUNTIME_PIP)
    udf = build_clip_embedding_udf(
        input_column=input_column,
        manifest=manifest,
        batch_size=batch_size,
        num_workers=num_workers,
        num_cpus=num_cpus,
        num_gpus=resolved_gpus,
        memory_bytes=memory_bytes,
        checkpoint_size=checkpoint_size,
        task_size=task_size,
        model_name=model_name,
        pretrained=pretrained,
        dim=dim,
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
            reset=reset,
        )
    logger.info("frame_embed_ok")
