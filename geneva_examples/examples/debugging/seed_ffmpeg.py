"""ffmpeg-availability demo: seed a tiny table, backfill with ffmpeg_probe.

Only meaningful with --mode enterprise: local Ray workers share the driver's
own environment (build_manifest() returns None locally), so a local run only
proves your laptop has ffmpeg on PATH, not that the cluster's conda manifest
resolves it. See geneva_examples/examples/debugging/ffmpeg_probe.py and
build_manifest() in geneva_examples/core/common.py.
"""

from __future__ import annotations

import logging

from geneva_examples.core.backfill import backfill_column
from geneva_examples.core.common import (
    OPT_STABLE_ROW_IDS,
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
    table_name: str = "ffmpeg_probe_demo",
    rows: int = 4,
    concurrency: int = 2,
    task_size: int = 4,
    checkpoint_size: int = 4,
    backfill_timeout_min: int = 15,
    flush_interval_s: float = 5.0,
    schema_wait_attempts: int = 30,
    schema_wait_sleep_s: int = 2,
) -> None:
    """Seed `table_name`, backfill an `ffmpeg_version` column, and print it."""
    import geneva

    logger.info("geneva_version %s mode %s", geneva.__version__, cfg.mode)
    logger.info("db_uri %s table %s", cfg.db_uri, table_name)
    if cfg.is_local:
        logger.warning(
            "running in local mode: this only proves your laptop has ffmpeg on "
            "PATH, not that the cluster's conda manifest resolves it. Pass "
            "--mode enterprise to actually test the shared build_manifest() change."
        )

    conn = connect(cfg)
    table = conn.create_table(
        table_name,
        data=[{"id": i} for i in range(1, rows + 1)],
        mode="overwrite",
        storage_options={OPT_STABLE_ROW_IDS: "true"},
    )
    logger.info("seeded %d rows into %s", rows, table_name)

    from geneva_examples.examples.debugging.ffmpeg_probe import (
        FFMPEG_PROBE_RUNTIME_PIP,
        build_ffmpeg_probe_udf,
    )

    manifest = build_manifest(cfg, "ffmpeg-probe", FFMPEG_PROBE_RUNTIME_PIP)
    udf = build_ffmpeg_probe_udf(input_column="id", manifest=manifest)

    with runtime_session(conn, cfg):
        table = backfill_column(
            conn=conn,
            table=table,
            table_name=table_name,
            column="ffmpeg_version",
            udf=udf,
            concurrency=concurrency,
            task_size=task_size,
            checkpoint_size=checkpoint_size,
            flush_interval_s=flush_interval_s,
            timeout_min=backfill_timeout_min,
            wait_attempts=schema_wait_attempts,
            wait_sleep_s=schema_wait_sleep_s,
            use_cpu_only_pool=True,
        )

    logger.info(
        "\n%s",
        format_sample(
            table.search().select(["id", "ffmpeg_version"]).limit(rows).to_list()
        ),
    )
    logger.info("ffmpeg_probe_ok")
