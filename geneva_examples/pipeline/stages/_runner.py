"""Shared single-column backfill orchestration for stage CLIs."""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import TYPE_CHECKING

from geneva_examples.core.utils.tables import wait_for_columns

if TYPE_CHECKING:
    from geneva_examples.core._types import ConnectionLike, TableLike

logger = logging.getLogger(__name__)


def backfill_column(
    *,
    conn: ConnectionLike,
    table: TableLike,
    table_name: str,
    column: str,
    udf: object,
    concurrency: int,
    task_size: int,
    checkpoint_size: int,
    flush_interval_s: float,
    timeout_min: int,
    wait_attempts: int,
    wait_sleep_s: int,
    use_cpu_only_pool: bool = False,
) -> TableLike:
    """Drop/add ``column`` backed by ``udf``, wait for it, backfill, and log."""
    try:
        table.drop_columns([column])
    except Exception:  # noqa: BLE001
        pass

    table.add_columns({column: udf})
    table = wait_for_columns(
        conn=conn,
        table_name=table_name,
        required={column},
        attempts=wait_attempts,
        sleep_s=wait_sleep_s,
    )

    start = time.perf_counter()
    job = table.backfill(
        column,
        concurrency=concurrency,
        task_size=task_size,
        checkpoint_size=checkpoint_size,
        batch_checkpoint_flush_interval_seconds=flush_interval_s,
        use_cpu_only_pool=use_cpu_only_pool,
        timeout=timedelta(minutes=timeout_min),
    )
    logger.info("job %s %s", column, job.job_id)
    logger.info("backfill_seconds %s %s", column, round(time.perf_counter() - start, 3))

    table.checkout_latest()
    logger.info("null_%s %s", column, table.count_rows(f"`{column}` IS NULL"))
    return table
