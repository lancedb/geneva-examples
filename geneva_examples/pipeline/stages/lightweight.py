"""Lightweight feature stage: file size + image dimensions (CPU only)."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import typer

from geneva_examples.core.common import connect, setup_logging
from geneva_examples.core.config import load_config
from geneva_examples.pipeline.stages._runner import backfill_column

logger = logging.getLogger(__name__)

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def run(
    config: Path | None = typer.Option(None, "--config", help="Path to config.yaml."),
    log_level: str = typer.Option("INFO", help="Logging level."),
    db_uri: str | None = typer.Option(None, help="Override config db_uri."),
    table_name: str | None = typer.Option(None, help="Override config table_name."),
    backfill_timeout_min: int = typer.Option(1000, help="Per-backfill timeout (min)."),
    backfill_concurrency: int = typer.Option(32, help="Backfill concurrency."),
    backfill_task_size: int = typer.Option(256, help="Backfill task size."),
    backfill_checkpoint_size: int = typer.Option(128, help="Backfill checkpoint size."),
    backfill_flush_interval_s: float = typer.Option(
        30.0, help="Batch checkpoint flush interval (seconds)."
    ),
    use_cpu_only_pool: bool = typer.Option(True, help="Use the CPU-only pool."),
    schema_wait_attempts: int = typer.Option(30, help="Schema-visibility attempts."),
    schema_wait_sleep_s: int = typer.Option(2, help="Seconds between schema checks."),
) -> None:
    """Add file_size + dimensions columns to the configured table."""
    setup_logging(log_level)
    import geneva
    from geneva.manifest import GenevaManifest

    from geneva_examples.udfs.imageinfo import (
        IMAGEINFO_RUNTIME_PIP,
        build_dimensions_udf,
        build_file_size_udf,
    )

    cfg = load_config(config)
    if db_uri:
        cfg.db_uri = db_uri
    if table_name:
        cfg.table_name = table_name

    logger.info("geneva_version %s", geneva.__version__)
    logger.info("db_uri %s table %s", cfg.db_uri, cfg.table_name)

    conn = connect(cfg)
    table = conn.open_table(cfg.table_name)

    manifest = (
        GenevaManifest.create_pip(f"light-{uuid.uuid4().hex[:6]}")
        .pip(IMAGEINFO_RUNTIME_PIP)
        .build()
    )
    columns = {
        "file_size": build_file_size_udf(input_column="image", manifest=manifest),
        "dimensions": build_dimensions_udf(input_column="image", manifest=manifest),
    }
    for column, udf in columns.items():
        table = backfill_column(
            conn=conn,
            table=table,
            table_name=cfg.table_name,
            column=column,
            udf=udf,
            concurrency=backfill_concurrency,
            task_size=backfill_task_size,
            checkpoint_size=backfill_checkpoint_size,
            flush_interval_s=backfill_flush_interval_s,
            timeout_min=backfill_timeout_min,
            wait_attempts=schema_wait_attempts,
            wait_sleep_s=schema_wait_sleep_s,
            use_cpu_only_pool=use_cpu_only_pool,
        )

    logger.info(
        "feature_sample %s",
        table.search()
        .select(["image_id", "label", "file_size", "dimensions"])
        .limit(5)
        .to_list(),
    )
    logger.info("lightweight_ok")


if __name__ == "__main__":
    app()
