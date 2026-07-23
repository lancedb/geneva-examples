"""Error-demo step: run a faulty backfill, then analyze it in the TUI.

Seeds a small ``(id, value)`` table and backfills a ``score`` column with a
UDF that deterministically fails on some rows (see ``faulty.py``). Because
the UDF skips failures, the job finishes DONE while the failed rows land as
NULLs in ``score`` and as records in the ``geneva_errors`` system table —
real material for the table viewer: ``uv run tui`` -> Tables ->
``geneva_errors (system)``, then filter on the printed job id.
"""

from __future__ import annotations

import logging
from collections import Counter

from geneva_examples.core.backfill import backfill_column
from geneva_examples.core.common import build_manifest, connect, runtime_session
from geneva_examples.core.config import Config

logger = logging.getLogger(__name__)


def _latest_job_id(conn: object, table_name: str) -> str | None:
    """Best-effort id of the demo's backfill job, for the next-steps text."""
    try:
        jobs = [
            jr
            for status in ("DONE", "RUNNING", "FAILED")
            for jr in conn.list_jobs(table_name=table_name, status=status)
        ]
    except Exception as exc:  # noqa: BLE001 — cosmetic only
        logger.warning("list_jobs failed: %s", exc)
        return None
    if not jobs:
        return None
    jobs.sort(key=lambda jr: getattr(jr, "launched_at", None) or 0)
    return getattr(jobs[-1], "job_id", None)


def run(
    cfg: Config,
    *,
    table_name: str = "debug_demo",
    rows: int = 40,
    fail_every: int = 7,
    concurrency: int = 2,
    task_size: int = 32,
    checkpoint_size: int = 32,
    backfill_timeout_min: int = 30,
    flush_interval_s: float = 5.0,
    schema_wait_attempts: int = 30,
    schema_wait_sleep_s: int = 2,
) -> None:
    """Seed ``table_name``, backfill the faulty ``score`` column, and report."""
    import geneva

    from geneva_examples.examples.debugging.faulty import (
        FAULTY_RUNTIME_PIP,
        build_faulty_score_udf,
    )

    logger.info("geneva_version %s mode %s", geneva.__version__, cfg.mode)
    logger.info("db_uri %s table %s rows %d", cfg.db_uri, table_name, rows)
    if not cfg.is_local:
        logger.warning(
            "running against the enterprise cluster: this demo (over)writes "
            "table %r there — pass --mode local for a laptop-only run",
            table_name,
        )

    conn = connect(cfg)

    # Fresh, deterministic input: value == id, so you can predict from the
    # error message exactly which rows will fail.
    data = [{"id": i, "value": i} for i in range(1, rows + 1)]
    table = conn.create_table(table_name, data, mode="overwrite")
    logger.info("seeded %d rows into %s", rows, table_name)

    manifest = build_manifest(cfg, "debug-demo", FAULTY_RUNTIME_PIP)
    udf = build_faulty_score_udf(
        input_column="value", fail_every=fail_every, manifest=manifest
    )

    with runtime_session(conn, cfg):
        table = backfill_column(
            conn=conn,
            table=table,
            table_name=table_name,
            column="score",
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

    # The job "succeeded" — now show the holes it left behind. Scope the
    # error-store read to this job: geneva_errors is append-only, so the demo
    # table accumulates records across re-runs.
    job_id = _latest_job_id(conn, table_name)
    nulls = table.count_rows("score IS NULL")
    try:
        errors = table.get_errors(job_id=job_id) if job_id else table.get_errors()
    except Exception as exc:  # noqa: BLE001 — error store is best-effort
        logger.warning("error store unavailable: %s", exc)
        errors = []
    logger.info("null_score %d error_records %d", nulls, len(errors))
    by_type = Counter(
        str(getattr(e, "error_type", None) or "UnknownError") for e in errors
    )
    for etype, count in by_type.most_common():
        logger.info("  %s x%d", etype, count)

    shown_id = job_id or "<job-id>"
    logger.info(
        "\nanalyze it in the table viewer:\n"
        "  uv run tui   -> Tables -> geneva_errors (system)\n"
        "                 paste the job id into the job_id filter:\n"
        "                 %s\n"
        "                 (open %s too — the failed rows have NULL score)\n"
        "  uv run jobs show %s   # the raw job record\n",
        shown_id,
        table_name,
        shown_id,
    )
    logger.info("demo_errors_ok")
