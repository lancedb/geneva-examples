"""Lightweight feature step: file size + image dimensions (CPU only)."""

from __future__ import annotations

import logging

from geneva_examples.core.backfill import backfill_column
from geneva_examples.core.common import (
    build_manifest,
    connect,
    format_sample,
    runtime_session,
)
from geneva_examples.core.config import Config

logger = logging.getLogger(__name__)


def run(
    cfg: Config,
    *,
    table_name: str = "images",
    backfill_timeout_min: int = 1000,
    backfill_concurrency: int = 32,
    backfill_task_size: int = 256,
    backfill_checkpoint_size: int = 128,
    backfill_flush_interval_s: float = 30.0,
    use_cpu_only_pool: bool = True,
    schema_wait_attempts: int = 30,
    schema_wait_sleep_s: int = 2,
) -> None:
    """Add file_size + dimensions columns to the configured table."""
    import geneva

    from geneva_examples.examples.images.imageinfo import (
        IMAGEINFO_RUNTIME_PIP,
        build_dimensions_udf,
        build_file_size_udf,
    )

    logger.info("geneva_version %s mode %s", geneva.__version__, cfg.mode)
    logger.info("db_uri %s table %s", cfg.db_uri, table_name)

    conn = connect(cfg)
    table = conn.open_table(table_name)

    manifest = build_manifest(cfg, "light", IMAGEINFO_RUNTIME_PIP)
    columns = {
        "file_size": build_file_size_udf(input_column="image", manifest=manifest),
        "dimensions": build_dimensions_udf(input_column="image", manifest=manifest),
    }
    with runtime_session(conn, cfg):
        for column, udf in columns.items():
            table = backfill_column(
                conn=conn,
                table=table,
                table_name=table_name,
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
        "feature_sample\n%s",
        format_sample(
            table.search()
            .select(["image_id", "label", "file_size", "dimensions"])
            .limit(5)
            .to_list()
        ),
    )
    logger.info("lightweight_ok")
