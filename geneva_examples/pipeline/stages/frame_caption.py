"""Frame caption CLI: BLIP captions on video_clips.frame (GPU)."""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

import typer

from geneva_examples.core.common import connect, memory_request_bytes, setup_logging
from geneva_examples.core.config import load_config
from geneva_examples.pipeline.stages._runner import backfill_column

logger = logging.getLogger(__name__)

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def run(
    config: Path | None = typer.Option(None, "--config", help="Path to config.yaml."),
    log_level: str = typer.Option("INFO", help="Logging level."),
    db_uri: str | None = typer.Option(None, help="Override config db_uri."),
    table_name: str = typer.Option("video_clips", help="Table to operate on."),
    input_column: str = typer.Option("frame", help="Column of encoded images."),
    output_column: str = typer.Option("caption", help="Output caption column."),
    batch_size: int = typer.Option(256, help="DataLoader batch size."),
    num_workers: int = typer.Option(8, help="DataLoader worker processes."),
    num_cpus: float = typer.Option(4.0, help="CPUs per model task."),
    num_gpus: float | None = typer.Option(None, help="GPUs per task (default 0.5)."),
    memory_gib: int = typer.Option(1, help="Memory (GiB) per task (geneva caps <2)."),
    checkpoint_size: int = typer.Option(4096, help="Rows per UDF __call__."),
    task_size: int = typer.Option(4096, help="Rows per read task."),
    concurrency: int = typer.Option(16, help="Backfill concurrency."),
    backfill_timeout_min: int = typer.Option(1000, help="Per-backfill timeout (min)."),
    flush_interval_s: float = typer.Option(30.0, help="Checkpoint flush interval (s)."),
    schema_wait_attempts: int = typer.Option(30, help="Schema-visibility attempts."),
    schema_wait_sleep_s: int = typer.Option(2, help="Seconds between schema checks."),
) -> None:
    """Add a BLIP caption column to the frames table."""
    setup_logging(log_level)
    os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")

    import geneva
    from geneva.manifest import GenevaManifest

    from geneva_examples.udfs.blip import BLIP_RUNTIME_PIP, build_blip_caption_udf

    cfg = load_config(config)
    if db_uri:
        cfg.db_uri = db_uri
    cfg.table_name = table_name
    resolved_gpus = num_gpus if num_gpus is not None else 0.5

    logger.info("geneva_version %s", geneva.__version__)
    logger.info(
        "db_uri %s table %s column %s", cfg.db_uri, cfg.table_name, input_column
    )
    logger.info(
        "batch_size %s num_workers %s num_gpus %s",
        batch_size,
        num_workers,
        resolved_gpus,
    )

    conn = connect(cfg)
    table = conn.open_table(cfg.table_name)

    manifest = (
        GenevaManifest.create_pip(f"frame-caption-{uuid.uuid4().hex[:6]}")
        .pip(BLIP_RUNTIME_PIP)
        .build()
    )
    udf = build_blip_caption_udf(
        input_column=input_column,
        manifest=manifest,
        batch_size=batch_size,
        num_workers=num_workers,
        num_cpus=num_cpus,
        num_gpus=resolved_gpus,
        memory_bytes=memory_request_bytes(memory_gib),
        checkpoint_size=checkpoint_size,
        task_size=task_size,
    )
    table = backfill_column(
        conn=conn,
        table=table,
        table_name=cfg.table_name,
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
    logger.info(
        "caption_sample %s",
        table.search()
        .select(["video_id", "chunk_id", output_column])
        .limit(5)
        .to_list(),
    )
    logger.info("frame_caption_ok")


if __name__ == "__main__":
    app()
