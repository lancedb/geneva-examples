"""Caption step: BLIP image captions (GPU; CPU in local mode)."""

from __future__ import annotations

import logging
import os
from io import BytesIO
from typing import Any

from geneva_examples.core.backfill import backfill_column
from geneva_examples.core.common import (
    build_manifest,
    connect,
    format_sample,
    local_or,
    resolve_resources,
    runtime_session,
)
from geneva_examples.core.config import Config

logger = logging.getLogger(__name__)


def _local_caption_preview(table: Any, model_id: str) -> None:
    """Run BLIP locally on one image as a sanity check."""
    import torch
    from PIL import Image
    from transformers import BlipForConditionalGeneration, BlipProcessor

    row = table.search().select(["image"]).limit(1).to_list()
    if not row:
        return
    processor = BlipProcessor.from_pretrained(model_id)
    model = BlipForConditionalGeneration.from_pretrained(model_id)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.to(device)  # ty: ignore[invalid-argument-type]  # third-party stub gap
    img = Image.open(BytesIO(row[0]["image"])).convert("RGB")
    inputs = {k: v.to(device) for k, v in processor([img], return_tensors="pt").items()}
    out = model.generate(**inputs, max_length=50)
    logger.info(
        "caption_local_preview %s", processor.decode(out[0], skip_special_tokens=True)
    )


def run(
    cfg: Config,
    *,
    table_name: str = "images",
    batch_size: int = 1024,
    num_workers: int = 8,
    num_cpus: float = 8.0,
    num_gpus: float | None = None,
    memory_gib: int = 1,
    checkpoint_size: int = 1024,
    task_size: int = 1024,
    concurrency: int = 8,
    backfill_timeout_min: int = 1000,
    flush_interval_s: float = 30.0,
    caption_local_preview: bool = False,
    schema_wait_attempts: int = 30,
    schema_wait_sleep_s: int = 2,
) -> None:
    """Add a `caption_blip` column to the configured table."""
    os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")

    import geneva

    from geneva_examples.examples._shared.blip import (
        BLIP_RUNTIME_PIP,
        build_blip_caption_udf,
    )

    resolved_gpus = num_gpus if num_gpus is not None else 1.0
    num_cpus, resolved_gpus, memory_bytes = resolve_resources(
        cfg, num_cpus=num_cpus, num_gpus=resolved_gpus, memory_gib=memory_gib
    )
    batch_size = local_or(cfg, 8, batch_size)
    num_workers = local_or(cfg, 0, num_workers)
    concurrency = local_or(cfg, 1, concurrency)

    logger.info("geneva_version %s mode %s", geneva.__version__, cfg.mode)
    logger.info("db_uri %s table %s", cfg.db_uri, table_name)

    conn = connect(cfg)
    table = conn.open_table(table_name)

    if caption_local_preview:
        _local_caption_preview(table, "Salesforce/blip-image-captioning-base")

    manifest = build_manifest(cfg, "caption", BLIP_RUNTIME_PIP)
    udf = build_blip_caption_udf(
        input_column="image",
        manifest=manifest,
        batch_size=batch_size,
        num_workers=num_workers,
        num_cpus=num_cpus,
        num_gpus=resolved_gpus,
        memory_bytes=memory_bytes,
        checkpoint_size=checkpoint_size,
        task_size=task_size,
    )
    with runtime_session(conn, cfg):
        table = backfill_column(
            conn=conn,
            table=table,
            table_name=table_name,
            column="caption_blip",
            udf=udf,
            concurrency=concurrency,
            task_size=task_size,
            checkpoint_size=checkpoint_size,
            flush_interval_s=flush_interval_s,
            timeout_min=backfill_timeout_min,
            wait_attempts=schema_wait_attempts,
            wait_sleep_s=schema_wait_sleep_s,
        )

    logger.info(
        "caption_sample\n%s",
        format_sample(
            table.search().select(["image_id", "caption_blip"]).limit(5).to_list()
        ),
    )
    logger.info("captions_ok")
