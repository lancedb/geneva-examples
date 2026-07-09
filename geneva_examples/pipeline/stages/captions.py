"""Caption stage: BLIP image captions, two UDF variants (GPU)."""

from __future__ import annotations

import logging
import os
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any

import typer

from geneva_examples.core.common import connect, memory_request_bytes, setup_logging
from geneva_examples.core.config import load_config
from geneva_examples.pipeline.stages._runner import backfill_column

logger = logging.getLogger(__name__)

app = typer.Typer(add_completion=False, help=__doc__)


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


@app.command()
def run(
    config: Path | None = typer.Option(None, "--config", help="Path to config.yaml."),
    log_level: str = typer.Option("INFO", help="Logging level."),
    db_uri: str | None = typer.Option(None, help="Override config db_uri."),
    table_name: str = typer.Option("images", help="Table to operate on."),
    batch_size: int = typer.Option(1024, help="DataLoader batch size."),
    num_workers: int = typer.Option(8, help="DataLoader worker processes."),
    num_cpus: float = typer.Option(8.0, help="CPUs per model task."),
    num_gpus: float | None = typer.Option(None, help="GPUs per task (default 1.0)."),
    memory_gib: int = typer.Option(1, help="Memory (GiB) per task (geneva caps <2)."),
    checkpoint_size: int = typer.Option(1024, help="Rows per UDF __call__."),
    task_size: int = typer.Option(1024, help="Rows per read task."),
    concurrency: int = typer.Option(8, help="Backfill concurrency."),
    backfill_timeout_min: int = typer.Option(1000, help="Per-backfill timeout (min)."),
    flush_interval_s: float = typer.Option(30.0, help="Checkpoint flush interval (s)."),
    caption_local_preview: bool = typer.Option(
        False, help="Log a local BLIP caption for one image before backfill."
    ),
    schema_wait_attempts: int = typer.Option(30, help="Schema-visibility attempts."),
    schema_wait_sleep_s: int = typer.Option(2, help="Seconds between schema checks."),
) -> None:
    """Add `caption_blip` and `caption_blip_v2` columns to the configured table."""
    setup_logging(log_level)
    os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")

    import geneva
    from geneva.manifest import GenevaManifest

    from geneva_examples.udfs.blip import BLIP_RUNTIME_PIP, build_blip_caption_udf

    cfg = load_config(config)
    if db_uri:
        cfg.db_uri = db_uri
    resolved_gpus = num_gpus if num_gpus is not None else 1.0

    logger.info("geneva_version %s", geneva.__version__)
    logger.info("db_uri %s table %s", cfg.db_uri, table_name)

    conn = connect(cfg)
    table = conn.open_table(table_name)

    if caption_local_preview:
        _local_caption_preview(table, "Salesforce/blip-image-captioning-base")

    manifest = (
        GenevaManifest.create_pip(f"caption-{uuid.uuid4().hex[:6]}")
        .pip(BLIP_RUNTIME_PIP)
        .build()
    )

    def _make_udf():
        return build_blip_caption_udf(
            input_column="image",
            manifest=manifest,
            batch_size=batch_size,
            num_workers=num_workers,
            num_cpus=num_cpus,
            num_gpus=resolved_gpus,
            memory_bytes=memory_request_bytes(memory_gib),
            checkpoint_size=checkpoint_size,
            task_size=task_size,
        )

    for column in ("caption_blip", "caption_blip_v2"):
        table = backfill_column(
            conn=conn,
            table=table,
            table_name=table_name,
            column=column,
            udf=_make_udf(),
            concurrency=concurrency,
            task_size=task_size,
            checkpoint_size=checkpoint_size,
            flush_interval_s=flush_interval_s,
            timeout_min=backfill_timeout_min,
            wait_attempts=schema_wait_attempts,
            wait_sleep_s=schema_wait_sleep_s,
        )

    logger.info(
        "caption_sample %s",
        table.search()
        .select(["image_id", "caption_blip", "caption_blip_v2"])
        .limit(5)
        .to_list(),
    )
    logger.info("captions_ok")


if __name__ == "__main__":
    app()
