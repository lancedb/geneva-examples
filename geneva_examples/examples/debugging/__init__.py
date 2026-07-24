"""Debugging demo — generate real per-row errors, then analyze them.

One step seeds a small table and backfills a deliberately faulty UDF with
``skip_on_error``: the job completes DONE while the failing rows are written
as NULLs and recorded in the ``geneva_errors`` system table. That leaves real
material to analyze in the TUI table viewer (``uv run tui`` -> Tables ->
``geneva_errors (system)``) or with ``uv run jobs show <job_id>``.
"""

from __future__ import annotations

from geneva_examples.core.spec import Example, Param, Step
from geneva_examples.examples.debugging import seed_errors

DEMO_ERRORS = Step(
    key="demo-errors",
    title="Generate debuggable errors",
    description=(
        "Seed a small `(id, value)` table, then backfill a `score` column with "
        "a UDF that deterministically fails on some rows (divisible-by-N -> "
        "ValueError, ends-in-9 -> TimeoutError) under `skip_on_error`. The job "
        "finishes DONE with NULL holes and real `geneva_errors` records — then "
        "analyze them in the Tables viewer or with `uv run jobs show <job_id>`."
    ),
    run=seed_errors.run,
    default_mode="local",  # laptop-first demo; pass --mode enterprise to opt out
    params=(
        Param("table_name", str, "debug_demo", "Demo table to (over)write."),
        Param("rows", int, 40, "Rows to seed (value == id).", min=1),
        Param(
            "fail_every",
            int,
            7,
            "Values divisible by this raise ValueError (0 disables).",
            min=0,
        ),
        Param("concurrency", int, 2, "Backfill concurrency.", min=1),
        Param("task_size", int, 32, "Rows per read task.", min=1),
        Param("checkpoint_size", int, 32, "Rows per UDF checkpoint.", min=1),
        Param("backfill_timeout_min", int, 30, "Backfill timeout (min).", min=1),
        Param("flush_interval_s", float, 5.0, "Checkpoint flush interval (s).", min=0),
        Param("schema_wait_attempts", int, 30, "Schema-visibility attempts.", min=1),
        Param("schema_wait_sleep_s", int, 2, "Seconds between checks.", min=0),
    ),
)

EXAMPLE = Example(
    name="debugging",
    title="Debugging demo",
    description=__doc__ or "",
    modality="demo",
    steps=(DEMO_ERRORS,),
)
