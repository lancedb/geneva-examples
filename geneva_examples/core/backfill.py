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
    reset: bool = True,
) -> TableLike:
    """Add ``column`` backed by ``udf``, wait for it, backfill, and log.

    ``reset`` controls what happens when the column already exists:

    - ``True`` (default): **drop and recompute**. The column is dropped so
      ``add_columns`` re-binds it to the current ``udf`` and the backfill
      recomputes every row. Destructive — wipes prior values — but guarantees
      the whole column reflects the current UDF/model. This is a *schema change*,
      so it must not run concurrently with another job appending rows to the
      same table.
    - ``False``: **incremental**. Keep the existing column and fill only the
      rows still missing it (``table.backfill`` defaults to ``<column> IS NULL``),
      using the column's already-registered UDF. Safe to run repeatedly — each
      pass picks up whatever rows landed since the last one. On first run (column
      absent) both modes behave identically. NOTE: this cannot overlap with a job
      still *appending* rows to the same table — adding the column is a schema
      change that breaks the producer's schema-matched appends, so run this only
      once the producer (e.g. a chunk refresh) has finished.
    """
    column_exists = column in set(table.schema.names)
    if reset and column_exists:
        # Explicit rebuild requested: drop so add_columns rebinds to the current
        # UDF and the backfill below recomputes every row. Tolerate a drop that
        # fails (e.g. the column vanished between the check and here) — add_columns
        # then surfaces any real problem.
        try:
            table.drop_columns([column])
        except Exception:  # noqa: BLE001
            pass
        column_exists = False

    if not column_exists:
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

    # Both modes rely on the column's registered UDF binding (set by add_columns
    # above), never a backfill(udf=...) override — that override is not supported
    # for remote/enterprise connections (backfill_async raises NotImplementedError;
    # you'd have to alter_columns() first). Consequence: an incremental re-run
    # (reset=False) on a pre-existing column keeps that column's original UDF for
    # the null rows. To swap the UDF/model, use reset=True to rebuild the column.
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
