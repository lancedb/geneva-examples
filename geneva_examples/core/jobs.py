"""Shared read/format helpers for Geneva job records.

A job record is the only progress-and-post-mortem surface a backfill exposes
(geneva has no streaming log API), and two places read it: the ``jobs`` CLI
(:mod:`geneva_examples.ops.jobs`) and the TUI's **Jobs** view. The querying and
rendering live here so both always show the same fields the same way.

Every accessor goes through ``getattr`` with a fallback on purpose: the record
is a geneva-owned object whose shape can shift across pins, and a field that
disappears should degrade to ``-`` rather than raise halfway through a render.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Only these two mean "still going"; the rest are end states.
ACTIVE_STATUSES = ["RUNNING", "PENDING"]
ALL_STATUSES = ["PENDING", "RUNNING", "DONE", "FAILED", "CANCELLED"]
TERMINAL_STATUSES = frozenset({"DONE", "FAILED", "CANCELLED"})


def job_status(jr: object) -> str:
    """The job's status as a plain string (geneva stores it as an enum)."""
    s = getattr(jr, "status", None)
    return getattr(s, "value", str(s))


def job_target(jr: object) -> str:
    """``table.column`` — the column this job is computing."""
    return f"{getattr(jr, 'table_name', '-')}.{getattr(jr, 'column_name', '-')}"


def format_dt(value: object) -> str:
    """A UTC ``YYYY-MM-DD HH:MM:SS`` stamp, or ``-`` if absent/not a datetime."""
    if not isinstance(value, datetime):
        return "-"
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def elapsed(jr: object) -> str:
    """``H:MM:SS`` between launch and completion — or launch and now, if running."""
    start = getattr(jr, "launched_at", None)
    if not isinstance(start, datetime):
        return "-"
    end = getattr(jr, "completed_at", None)
    if not isinstance(end, datetime):
        end = datetime.now(UTC)
    secs = max(0, int((end - start).total_seconds()))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}"


def metrics_line(jr: object) -> str:
    """One-line ``name n/total (pct)`` summary of a job's metrics, or '' if none.

    The percentage is the only read on "how far along is this?" a running job
    offers, so it is computed here rather than left to each caller — and skipped
    when the counters aren't numbers (geneva leaves them unset early on).
    """
    parts = []
    for m in getattr(jr, "metrics", None) or []:
        name = getattr(m, "name", "?")
        n, total = getattr(m, "n", "?"), getattr(m, "total", "?")
        pct = ""
        if isinstance(n, (int, float)) and isinstance(total, (int, float)) and total:
            pct = f" ({n / total:.0%})"
        parts.append(f"{name} {n}/{total}{pct}")
    return "  ".join(parts)


# Ratio metrics that actually answer "how far along is this?", best first. The
# other row_* counters (checkpointed, ready_for_commit) are earlier stages of
# the same rows, so showing them next to rows_committed just repeats it.
_PROGRESS_METRICS = ("rows_committed", "tasks_completed", "fragments")


def progress_summary(jr: object, limit: int = 3) -> str:
    """A short ``name n/total (pct)`` progress line, or '' if nothing usable.

    A backfill record carries ~35 metrics, most of them timers and running
    totals whose ``total`` is 0 — those aren't progress. This keeps the
    ratio-shaped ones, prefers the few that read as progress, and caps the
    result so it fits on a line. The full set stays in :func:`format_detail`.
    """
    ratios: dict[str, str] = {}
    for m in getattr(jr, "metrics", None) or []:
        name = getattr(m, "name", "?")
        n, total = getattr(m, "n", None), getattr(m, "total", None)
        if (
            isinstance(n, (int, float))
            and isinstance(total, (int, float))
            and not isinstance(n, bool)
            and total > 0
        ):
            ratios[name] = f"{name} {n}/{total} ({n / total:.0%})"
    if not ratios:
        return ""
    preferred = [ratios[name] for name in _PROGRESS_METRICS if name in ratios]
    rest = [v for k, v in ratios.items() if k not in _PROGRESS_METRICS]
    return "  ".join((preferred or rest)[:limit])


def status_counts(jobs: list) -> str:
    """``2 RUNNING · 1 FAILED`` — a one-line tally for a list of job records."""
    counts: dict[str, int] = {}
    for jr in jobs:
        key = job_status(jr)
        counts[key] = counts.get(key, 0) + 1
    order = {s: i for i, s in enumerate(ALL_STATUSES)}
    ranked = sorted(counts.items(), key=lambda kv: order.get(kv[0], len(order)))
    return " · ".join(f"{n} {status}" for status, n in ranked)


def format_config(value: object) -> str:
    """Pretty-print the job's launch ``config``, which geneva stores as JSON text."""
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return value
    try:
        return json.dumps(value, indent=2, default=str, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def format_detail(jr: object, events_limit: int | None = 10) -> str:
    """Render a full job record as text.

    ``events_limit`` caps how many trailing events are included; pass ``None``
    for the complete append-only event log (useful when the root-cause event
    has scrolled past the tail).
    """
    lines = [
        f"job_id:     {getattr(jr, 'job_id', '-')}",
        f"status:     {job_status(jr)}",
        f"type:       {getattr(jr, 'job_type', '-')}",
        f"target:     {job_target(jr)}",
        f"cluster:    {getattr(jr, 'cluster_name', None) or '-'}",
        f"launched:   {format_dt(getattr(jr, 'launched_at', None))} "
        f"by {getattr(jr, 'launched_by', None) or '-'}",
        f"updated:    {format_dt(getattr(jr, 'updated_at', None))}",
        f"completed:  {format_dt(getattr(jr, 'completed_at', None))}",
        f"elapsed:    {elapsed(jr)}",
    ]

    object_ref = getattr(jr, "object_ref", None)
    if object_ref:
        lines.append(f"object_ref: {object_ref}")
    manifest_id = getattr(jr, "manifest_id", None)
    if manifest_id:
        checksum = getattr(jr, "manifest_checksum", None) or "-"
        lines.append(f"manifest:   {manifest_id} (checksum {checksum})")

    config = format_config(getattr(jr, "config", None))
    if config:
        lines.append("config:")
        lines += [f"    {line}" for line in config.splitlines()]

    metrics = getattr(jr, "metrics", None) or []
    if metrics:
        lines.append("metrics:")
        for m in metrics:
            name = getattr(m, "name", "?")
            n, total = getattr(m, "n", "?"), getattr(m, "total", "?")
            desc = getattr(m, "desc", "")
            lines.append(f"    {name}: {n}/{total} {desc}")

    events = getattr(jr, "events", None) or []
    if events:
        shown = events if events_limit is None else events[-events_limit:]
        hidden = len(events) - len(shown)
        label = f"events ({len(events)} total"
        label += f", showing last {len(shown)})" if hidden > 0 else ")"
        lines.append(f"{label}:")
        lines += [f"    {e}" for e in shown]

    return "\n".join(lines)


def list_jobs(conn: Any, table: str | None, statuses: list[str]) -> list:
    """Union jobs across statuses.

    Query per-status and merge by job_id so a status that errors is logged and
    skipped rather than sinking the whole listing. (In geneva 0.14.1b5
    ``JobStateManager.list_jobs`` guards its ``WHERE`` with ``if wheres:``, so a
    no-filter call is valid — the per-status loop is defensive, not required.)
    """
    merged: dict = {}
    for s in statuses:
        try:
            for jr in conn.list_jobs(table_name=table, status=s):
                merged[getattr(jr, "job_id", None) or id(jr)] = jr
        except Exception as exc:  # noqa: BLE001
            logger.warning("list_jobs(status=%s) failed: %s", s, exc)
    return list(merged.values())


def sort_newest_first(jobs: list) -> list:
    """Sort job records by launch time, newest first (undated ones last)."""
    jobs.sort(
        key=lambda j: (
            getattr(j, "launched_at", None) or datetime.min.replace(tzinfo=UTC)
        ),
        reverse=True,
    )
    return jobs
