"""Debug CLI: guided diagnosis for Geneva backfill/refresh jobs.

Companion to ``jobs`` (the raw record viewer). ``debug`` reads the same two
durable stores — the ``geneva_jobs`` record and the ``geneva_errors`` store —
samples progress to estimate throughput, applies the bottleneck heuristics
from the debugging guide, and prints the exact commands for the three log
surfaces (client / driver pod / Ray workers).

Works offline too: every subcommand accepts ``--replay demo_data/*.jsonl`` to
run against recorded job snapshots instead of a live cluster — useful for
demos and for learning the workflow before you have a failing job of your own.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from geneva_examples.core import diagnose as dx
from geneva_examples.ops.jobs import (
    _elapsed,
    _fmt_dt,
    _metrics_line,
    _open_connection,
)

logger = logging.getLogger(__name__)

app = typer.Typer(add_completion=False, help=__doc__)

_SEV_STYLE = {
    dx.SEV_CRIT: {"fg": "red", "bold": True},
    dx.SEV_WARN: {"fg": "yellow"},
    dx.SEV_INFO: {"fg": "cyan"},
}


# ---------------------------------------------------------------------------
# Data access (live vs. replay)
# ---------------------------------------------------------------------------


def _fetch_errors(conn: Any, record: object, job_id: str) -> list:
    """Best-effort read of the job's ``geneva_errors`` records."""
    table_name = getattr(record, "table_name", None)
    if not table_name:
        return []
    try:
        return list(conn.open_table(table_name).get_errors(job_id=job_id))
    except Exception as exc:  # noqa: BLE001 — the report degrades gracefully
        logger.warning("error store unavailable: %s", exc)
        return []


def _load_live(
    config: Path | None,
    mode: str | None,
    db_uri: str | None,
    log_level: str,
    job_id: str,
    sample_secs: float,
) -> tuple[object, list, float | None]:
    """Fetch the record (twice, when RUNNING) plus its error records."""
    _cfg, conn = _open_connection(config, db_uri, log_level, mode)
    try:
        record = conn.get_job(job_id)
    except ValueError:
        typer.secho(f"job {job_id} not found", fg="red", err=True)
        raise typer.Exit(code=1) from None

    rate = None
    if dx.status_of(record) == "RUNNING" and sample_secs > 0:
        first = dx.sample(record, at=datetime.now(UTC))
        typer.secho(
            f"sampling {dx.THROUGHPUT_METRIC} for {sample_secs:.0f}s to "
            "estimate throughput (skip with --sample-secs 0) ...",
            fg="bright_black",
        )
        time.sleep(sample_secs)
        record = conn.get_job(job_id)
        rate = dx.rate_per_second(first, dx.sample(record, at=datetime.now(UTC)))

    return record, _fetch_errors(conn, record, job_id), rate


def _load_replay(replay: Path) -> tuple[object, list, float | None]:
    """Final snapshot, its errors, and the whole-run replay throughput."""
    snapshots = dx.load_replay(str(replay))
    record = snapshots[-1]
    rate = None
    if len(snapshots) > 1:
        rate = dx.rate_per_second(dx.sample(snapshots[0]), dx.sample(record))
    errors = list(getattr(record, "errors", None) or [])
    return record, errors, rate


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _echo_header(record: object, rate: float | None) -> None:
    status = dx.status_of(record)
    phase = dx.phase_of(getattr(record, "events", None))
    status_fg = {
        "RUNNING": "green",
        "DONE": "green",
        "FAILED": "red",
        "CANCELLED": "yellow",
        "PENDING": "yellow",
    }.get(status, "white")

    typer.echo(f"job:        {getattr(record, 'job_id', '-')}")
    typer.echo(
        f"target:     {getattr(record, 'table_name', '-')}."
        f"{getattr(record, 'column_name', '-')}"
        f"   type: {getattr(record, 'job_type', '-')}"
    )
    typer.secho(f"status:     {status}", fg=status_fg, bold=True, nl=False)
    typer.echo(f"   phase: {phase or '-'}")
    typer.echo(
        f"launched:   {_fmt_dt(getattr(record, 'launched_at', None))} by "
        f"{getattr(record, 'launched_by', None) or '-'}   elapsed: {_elapsed(record)}"
    )
    metrics = _metrics_line(record)
    if metrics:
        typer.echo(f"metrics:    {metrics}")
    if rate is not None:
        typer.echo(f"throughput: ~{rate:.1f} rows/s ({dx.THROUGHPUT_METRIC})")


def _echo_events(record: object, limit: int) -> None:
    events = getattr(record, "events", None) or []
    if not events:
        return
    shown = events[-limit:]
    typer.echo(f"events (last {len(shown)} of {len(events)}):")
    for event in shown:
        typer.echo(f"    {event}")


def _echo_error_summary(record: object, errors: list) -> None:
    if not errors:
        return
    typer.secho(f"errors:     {len(errors)} recorded in geneva_errors", fg="yellow")
    for etype, count, message in dx.summarize_errors(errors)[:3]:
        line = f"    {etype} x{count}"
        if message:
            line += f" — {message[:70]}"
        typer.echo(line)
    job_id = getattr(record, "job_id", "<job-id>")
    typer.echo(f"    full tracebacks: uv run debug errors {job_id} --trace")


def _echo_findings(findings: list[dx.Finding]) -> None:
    typer.secho("\nDIAGNOSIS", bold=True)
    if not findings:
        typer.echo("    nothing notable — see NEXT STEPS to dig manually")
        return
    for f in findings:
        style = _SEV_STYLE.get(f.severity, {})
        typer.secho(f"  [{f.severity.upper():4}] {f.signal}", **style)
        typer.echo(f"         cause: {f.diagnosis}")
        typer.echo(f"         next:  {f.action}")


def _echo_log_commands(kube_context: str, namespace: str) -> None:
    typer.secho("\nNEXT STEPS — the three log surfaces", bold=True)
    typer.echo(
        "  client: re-run with --log-level DEBUG to stream ray/worker "
        "detail into your terminal"
    )
    for title, command in dx.log_commands(
        kube_context=kube_context, namespace=namespace
    ):
        typer.secho(f"  # {title}", fg="bright_black")
        typer.echo(f"  {command}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

_CONFIG_OPT = typer.Option(None, "--config", help="Path to config.yaml.")
_MODE_OPT = typer.Option(
    None, "--mode", help="Connection mode: 'local' or 'enterprise'."
)
_DB_URI_OPT = typer.Option(None, help="Override config db_uri.")
_LOG_LEVEL_OPT = typer.Option("WARNING", help="Logging level (connection noise).")
_REPLAY_OPT = typer.Option(
    None,
    "--replay",
    help="Diagnose recorded snapshots (JSONL) instead of a live job.",
    exists=True,
    dir_okay=False,
)
_KUBE_CONTEXT_OPT = typer.Option(
    dx.DEFAULT_KUBE_CONTEXT, help="kubectl context of the cluster."
)
_NAMESPACE_OPT = typer.Option(
    dx.DEFAULT_NAMESPACE, help="Kubernetes namespace of the cluster."
)


def _report(
    job_id: str | None,
    config: Path | None,
    mode: str | None,
    db_uri: str | None,
    log_level: str,
    sample_secs: float,
    events_limit: int,
    replay: Path | None,
    kube_context: str,
    namespace: str,
) -> None:
    if replay is not None:
        record, errors, rate = _load_replay(replay)
    elif job_id:
        record, errors, rate = _load_live(
            config, mode, db_uri, log_level, job_id, sample_secs
        )
    else:
        typer.secho("pass a JOB_ID (or --replay <file>)", fg="red", err=True)
        raise typer.Exit(code=2)

    # Replayed snapshots are judged at their own recorded time, not wall time.
    now = getattr(record, "updated_at", None) if replay is not None else None
    _echo_header(record, rate)
    _echo_events(record, events_limit)
    _echo_error_summary(record, errors)
    _echo_findings(dx.diagnose(record, rate=rate, error_count=len(errors), now=now))
    _echo_log_commands(kube_context, namespace)


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Show help when invoked without a subcommand."""
    # A positional job-id here would swallow subcommand names, so the group
    # takes none: `debug report <job-id>` is the entry point.
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(code=0)


@app.command()
def report(
    job_id: str | None = typer.Argument(None, help="Job id to diagnose."),
    config: Path | None = _CONFIG_OPT,
    mode: str | None = _MODE_OPT,
    db_uri: str | None = _DB_URI_OPT,
    log_level: str = _LOG_LEVEL_OPT,
    sample_secs: float = typer.Option(
        15.0, help="Seconds to sample throughput on a RUNNING job (0 skips)."
    ),
    events_limit: int = typer.Option(8, help="Trailing events to show."),
    replay: Path | None = _REPLAY_OPT,
    kube_context: str = _KUBE_CONTEXT_OPT,
    namespace: str = _NAMESPACE_OPT,
) -> None:
    """Diagnose a job: record + throughput + errors + guided next steps."""
    _report(
        job_id,
        config,
        mode,
        db_uri,
        log_level,
        sample_secs,
        events_limit,
        replay,
        kube_context,
        namespace,
    )


@app.command()
def errors(
    job_id: str | None = typer.Argument(None, help="Job id to inspect."),
    config: Path | None = _CONFIG_OPT,
    mode: str | None = _MODE_OPT,
    db_uri: str | None = _DB_URI_OPT,
    log_level: str = _LOG_LEVEL_OPT,
    replay: Path | None = _REPLAY_OPT,
    trace: bool = typer.Option(
        False, "--trace", help="Print the full traceback of --index."
    ),
    index: int = typer.Option(0, help="Which error record --trace prints."),
) -> None:
    """List a job's per-row error records from the ``geneva_errors`` store."""
    if replay is not None:
        record, error_records, _ = _load_replay(replay)
        job_id = job_id or str(getattr(record, "job_id", "-"))
    elif job_id:
        _cfg, conn = _open_connection(config, db_uri, log_level, mode)
        try:
            record = conn.get_job(job_id)
        except ValueError:
            typer.secho(f"job {job_id} not found", fg="red", err=True)
            raise typer.Exit(code=1) from None
        error_records = _fetch_errors(conn, record, job_id)
    else:
        typer.secho("pass a JOB_ID (or --replay <file>)", fg="red", err=True)
        raise typer.Exit(code=2)

    if not error_records:
        typer.secho(f"no error records for job {job_id}", fg="green")
        return

    typer.secho(f"{len(error_records)} error record(s) for job {job_id}:", bold=True)
    for i, e in enumerate(error_records):
        typer.echo(
            f"  [{i}] {getattr(e, 'error_type', '?')}"
            f"  row={getattr(e, 'row_address', '-')}"
            f"  fragment={getattr(e, 'fragment_id', '-')}"
            f"  attempt={getattr(e, 'attempt', '-')}"
        )
        message = getattr(e, "error_message", None)
        if message:
            typer.echo(f"      {str(message)[:100]}")

    if trace:
        if not 0 <= index < len(error_records):
            typer.secho(f"--index {index} out of range", fg="red", err=True)
            raise typer.Exit(code=2)
        chosen = error_records[index]
        typer.secho(f"\ntraceback of [{index}]:", bold=True)
        typer.echo(getattr(chosen, "error_trace", None) or "(no traceback recorded)")
    else:
        typer.echo("\nre-run with --trace [--index N] for the full traceback")
        typer.echo(
            "retry only the failed rows: "
            "table.backfill(col, where='_rowaddr IN (<row addresses above>)')"
        )


@app.command()
def logs(
    kube_context: str = _KUBE_CONTEXT_OPT,
    namespace: str = _NAMESPACE_OPT,
) -> None:
    """Print retrieval commands for the driver-pod and Ray-worker logs."""
    typer.secho("Geneva job logs live in three places:", bold=True)
    typer.echo("  1. your terminal — the client (re-run with --log-level DEBUG)")
    typer.echo("  2. the driver pod — job-level orchestration failures")
    typer.echo("  3. Ray workers — everything your UDF printed/raised\n")
    for title, command in dx.log_commands(
        kube_context=kube_context, namespace=namespace
    ):
        typer.secho(f"# {title}", fg="bright_black")
        typer.echo(command)
    typer.echo(
        "\nRay logs are pod-local and vanish on autoscale-down; for "
        "post-mortems prefer the durable stores (jobs show / debug errors)."
    )


@app.command()
def watch(
    job_id: str | None = typer.Argument(None, help="Job id to watch."),
    config: Path | None = _CONFIG_OPT,
    mode: str | None = _MODE_OPT,
    db_uri: str | None = _DB_URI_OPT,
    log_level: str = _LOG_LEVEL_OPT,
    replay: Path | None = _REPLAY_OPT,
    refresh_secs: float = typer.Option(3.0, help="Poll interval."),
) -> None:
    """Live TUI dashboard: status, metrics, throughput, events, findings."""
    from geneva_examples.tui.debug_monitor import JobMonitorApp

    if replay is not None:
        source: Any = dx.ReplaySource(dx.load_replay(str(replay)))
        refresh_secs = min(refresh_secs, 1.0)  # replays should play briskly
    elif job_id:
        _cfg, conn = _open_connection(config, db_uri, log_level, mode)
        source = dx.LiveSource(conn, job_id)
    else:
        typer.secho("pass a JOB_ID (or --replay <file>)", fg="red", err=True)
        raise typer.Exit(code=2)

    JobMonitorApp(source, refresh_secs=refresh_secs).run()
