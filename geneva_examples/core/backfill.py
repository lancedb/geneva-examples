"""Shared single-column backfill orchestration for example pipeline steps.

Formerly ``pipeline/stages/_runner.py``; moved here as shared infra now that the
stages live inside self-contained example packages.
"""

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

    # The local (NativeTable) and remote (RemoteConnection) backfill APIs differ:
    # local runs on local Ray and has no `task_size`/`use_cpu_only_pool`/
    # `checkpoint_size` — the checkpoint knob is `max_checkpoint_size`, and the
    # worker-pool routing is remote-only.
    is_remote = bool(conn.is_remote())
    if is_remote:
        backfill_kwargs = dict(
            task_size=task_size,
            checkpoint_size=checkpoint_size,
            use_cpu_only_pool=use_cpu_only_pool,
        )
    else:
        # Local Ray has only this machine's cores. Cap concurrency (leaving a core
        # for the raylet/driver) and skip admission pre-flight so tasks queue for a
        # free slot instead of the job being rejected up front.
        from geneva_examples.core.common import local_concurrency

        concurrency = local_concurrency(concurrency)
        backfill_kwargs = dict(
            max_checkpoint_size=checkpoint_size,
            _admission_check=False,
        )

    start = time.perf_counter()
    job = table.backfill(
        column,
        concurrency=concurrency,
        batch_checkpoint_flush_interval_seconds=flush_interval_s,
        timeout=timedelta(minutes=timeout_min),
        **backfill_kwargs,
    )
    logger.info("job %s %s", column, job.job_id)
    logger.info("backfill_seconds %s %s", column, round(time.perf_counter() - start, 3))

    table.checkout_latest()
    logger.info("null_%s %s", column, table.count_rows(f"`{column}` IS NULL"))
    return table
