"""Jobs CLI: list and cancel Geneva backfill/refresh jobs on the cluster."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import typer

from geneva_examples.core.common import connect, setup_logging
from geneva_examples.core.config import Config, load_config
from geneva_examples.core.jobs import (
    ACTIVE_STATUSES,
    ALL_STATUSES,
    TERMINAL_STATUSES,
    elapsed,
    format_detail,
    format_dt,
    job_status,
    job_target,
    metrics_line,
    sort_newest_first,
)
from geneva_examples.core.jobs import list_jobs as query_jobs

logger = logging.getLogger(__name__)

app = typer.Typer(add_completion=False, help=__doc__)


def _open_connection(
    config: Path | None,
    db_uri: str | None,
    log_level: str,
    mode: str | None = None,
) -> tuple[Config, Any]:
    """Resolve config, apply ``db_uri`` override, and open a Geneva connection."""
    setup_logging(log_level)

    import geneva  # noqa: F401  (ensure importable before connect)

    cfg = load_config(config, mode_override=mode, db_uri_override=db_uri)
    return cfg, connect(cfg)


def _print_detail(jr: object, events_limit: int | None = 10) -> None:
    """Print a full job record (the same rendering the TUI's Jobs view shows)."""
    typer.echo(format_detail(jr, events_limit))


@app.callback(invoke_without_command=True)
def list_jobs(
    ctx: typer.Context,
    config: Path | None = typer.Option(None, "--config", help="Path to config.yaml."),
    mode: str | None = typer.Option(
        None, "--mode", help="Connection mode: 'local' or 'enterprise'."
    ),
    log_level: str = typer.Option("WARNING", help="Logging level (connection noise)."),
    db_uri: str | None = typer.Option(None, help="Override config db_uri."),
    job_id: str | None = typer.Option(
        None, help="Show full detail for a single job id (see also the 'show' command)."
    ),
    full_events: bool = typer.Option(
        False, "--full-events", help="With --job-id, print the entire event log."
    ),
    table: str | None = typer.Option(None, help="Filter by table name."),
    status: str | None = typer.Option(
        None, help="Filter by exact status (PENDING/RUNNING/DONE/FAILED/CANCELLED)."
    ),
    show_all: bool = typer.Option(
        False, "--all", help="Show all jobs (default: only PENDING/RUNNING)."
    ),
    limit: int = typer.Option(50, help="Max rows to display."),
) -> None:
    """List Geneva jobs (defaults to the active ones).

    Run with no subcommand to list; use ``kill <job_id>`` to cancel one.
    """
    if ctx.invoked_subcommand is not None:
        return

    cfg, conn = _open_connection(config, db_uri, log_level, mode)

    if job_id:
        try:
            jr = conn.get_job(job_id)
        except ValueError:
            typer.secho(f"job {job_id} not found on {cfg.db_uri}", fg="red", err=True)
            raise typer.Exit(code=1) from None
        _print_detail(jr, events_limit=None if full_events else 10)
        return

    if status:
        statuses = [status.upper()]
    elif show_all:
        statuses = ALL_STATUSES
    else:
        statuses = ACTIVE_STATUSES
    jobs = sort_newest_first(query_jobs(conn, table, statuses))

    scope = status or ("all" if show_all else "active (PENDING/RUNNING)")
    typer.echo(
        f"db_uri: {cfg.db_uri}   filter: {scope}   showing: {min(len(jobs), limit)}/{len(jobs)}"
    )
    if not jobs:
        typer.echo("  (no matching jobs)")
        return

    header = f"{'STATUS':<9} {'TYPE':<10} {'ELAPSED':>9}  {'LAUNCHED (UTC)':<19}  TARGET / JOB"
    typer.echo(header)
    typer.echo("-" * len(header))
    for jr in jobs[:limit]:
        typer.echo(
            f"{job_status(jr):<9} {getattr(jr, 'job_type', '-'):<10} {elapsed(jr):>9}  "
            f"{format_dt(getattr(jr, 'launched_at', None)):<19}  {job_target(jr)}  "
            f"{getattr(jr, 'job_id', '-')}"
        )


@app.command()
def show(
    job_id: str = typer.Argument(..., help="Job id to inspect."),
    config: Path | None = typer.Option(None, "--config", help="Path to config.yaml."),
    mode: str | None = typer.Option(
        None, "--mode", help="Connection mode: 'local' or 'enterprise'."
    ),
    log_level: str = typer.Option("WARNING", help="Logging level (connection noise)."),
    db_uri: str | None = typer.Option(None, help="Override config db_uri."),
    full_events: bool = typer.Option(
        False, "--full-events", help="Print the entire event log (not just the tail)."
    ),
) -> None:
    """Print the full record for a single job id.

    Equivalent to the top-level ``--job-id`` option, but takes the id as a
    positional argument so you can run ``jobs show <id>``.
    """
    cfg, conn = _open_connection(config, db_uri, log_level, mode)

    try:
        jr = conn.get_job(job_id)
    except ValueError:
        typer.secho(f"job {job_id} not found on {cfg.db_uri}", fg="red", err=True)
        raise typer.Exit(code=1) from None

    _print_detail(jr, events_limit=None if full_events else 10)


@app.command()
def kill(
    job_id: str = typer.Argument(..., help="Job id to cancel."),
    config: Path | None = typer.Option(None, "--config", help="Path to config.yaml."),
    mode: str | None = typer.Option(
        None, "--mode", help="Connection mode: 'local' or 'enterprise'."
    ),
    log_level: str = typer.Option("WARNING", help="Logging level (connection noise)."),
    db_uri: str | None = typer.Option(None, help="Override config db_uri."),
    force: bool = typer.Option(
        False, "--force", help="Mark CANCELLED even if the job is already terminal."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt."
    ),
) -> None:
    """Cancel a job by id, transitioning it to the CANCELLED terminal state.

    Geneva exposes no API to forcibly terminate already-running compute, so this
    flips the job record's status to CANCELLED in the ``_geneva_jobs`` system
    table. A PENDING job is stopped before it is dispatched; a RUNNING backfill's
    in-flight Ray tasks may keep going until they finish or time out, but the job
    will read as CANCELLED.
    """
    cfg, conn = _open_connection(config, db_uri, log_level, mode)

    try:
        jr = conn.get_job(job_id)
    except ValueError:
        typer.secho(f"job {job_id} not found on {cfg.db_uri}", fg="red", err=True)
        raise typer.Exit(code=1) from None

    status = job_status(jr)
    target = job_target(jr)

    if status in TERMINAL_STATUSES and not force:
        typer.echo(
            f"job {job_id} ({target}) is already {status}; nothing to cancel "
            "(use --force to mark CANCELLED anyway)."
        )
        return

    if not yes:
        typer.confirm(f"Cancel {status} job {job_id} ({target})?", abort=True)

    # geneva exposes no public cancel API, so we flip the job record via the
    # private history table. Guard the access: if a geneva pin bump renames or
    # removes it, fail with a clear message instead of a raw AttributeError.
    set_completed = getattr(getattr(conn, "_history", None), "set_completed", None)
    if not callable(set_completed):
        typer.secho(
            "this geneva build does not expose the private jobs-history API "
            "(conn._history.set_completed) that `kill` relies on; the geneva pin "
            "may have changed. Cannot cancel from the client.",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=1)

    set_completed(job_id, status="CANCELLED")
    typer.secho(f"marked job {job_id} CANCELLED", fg="yellow")
    _print_detail(conn.get_job(job_id))


@app.command()
def tail(
    job_id: str = typer.Argument(..., help="Job id to tail."),
    config: Path | None = typer.Option(None, "--config", help="Path to config.yaml."),
    mode: str | None = typer.Option(
        None, "--mode", help="Connection mode: 'local' or 'enterprise'."
    ),
    log_level: str = typer.Option("WARNING", help="Logging level (connection noise)."),
    db_uri: str | None = typer.Option(None, help="Override config db_uri."),
    interval: float = typer.Option(2.0, help="Poll interval (seconds)."),
    once: bool = typer.Option(
        False, help="Print the current state once and exit (no follow)."
    ),
) -> None:
    """Follow a job's event log until it reaches a terminal state.

    Geneva exposes no streaming log API, so this polls the job record's
    append-only ``events`` list (the closest thing to a log) every ``interval``
    seconds, printing each new event as it appears — and a metric-progress line
    whenever the counters change. It exits once the job is DONE/FAILED/CANCELLED,
    or on Ctrl-C.
    """
    cfg, conn = _open_connection(config, db_uri, log_level, mode)

    try:
        jr = conn.get_job(job_id)
    except ValueError:
        typer.secho(f"job {job_id} not found on {cfg.db_uri}", fg="red", err=True)
        raise typer.Exit(code=1) from None

    typer.echo(f"tailing job {job_id} ({job_target(jr)}) on {cfg.db_uri}")

    printed = 0  # events already emitted
    last_metrics = ""
    last_status: str | None = None

    try:
        while True:
            events = getattr(jr, "events", None) or []
            for ev in events[printed:]:
                typer.echo(f"  {ev}")
            printed = len(events)

            status = job_status(jr)
            if status != last_status:
                typer.secho(f"  [status: {status}]", fg="cyan")
                last_status = status

            metrics = metrics_line(jr)
            if metrics and metrics != last_metrics:
                typer.echo(f"  [metrics: {metrics}]")
                last_metrics = metrics

            if once or status in TERMINAL_STATUSES:
                break

            time.sleep(max(0.5, interval))
            jr = conn.get_job(job_id)
    except KeyboardInterrupt:
        typer.echo("")
        return

    typer.echo("")
    _print_detail(jr)


if __name__ == "__main__":
    app()
