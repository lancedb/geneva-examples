"""Pure job-diagnosis logic behind the ``debug`` ops CLI and TUI monitor.

Everything here operates on *record-like* objects — anything exposing the
``geneva_jobs`` record fields via attributes (a real geneva ``JobRecord``, a
test double, or a replay snapshot built by :func:`record_from_dict`) — so the
heuristics stay unit-testable without a cluster.

The rules encode the debugging guide that accompanies this repo:

- the job lifecycle and phase events written to the ``geneva_jobs`` system
  table (``Cluster provisioning`` -> ``Job planning`` -> ``Executing ...``),
- progress metrics read as rates (``workers``, ``rows_checkpointed``, ...),
- the bottleneck-signature table (workers below concurrency, stalled
  throughput, stale heartbeat, long provisioning),
- and the three log surfaces (client / driver pod / Ray workers) with their
  retrieval commands.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

# Lifecycle phase events geneva's driver appends to the job record's
# append-only ``events`` list.
PHASE_PROVISIONING = "Cluster provisioning"
PHASE_PLANNING = "Job planning"
PHASE_EXECUTING = "Executing"  # "Executing backfill" / "Executing refresh"
_PHASE_PREFIXES = (PHASE_PROVISIONING, PHASE_PLANNING, PHASE_EXECUTING)

# Progress metric geneva increments as UDF output is durably checkpointed —
# the closest thing to end-to-end throughput a backfill exposes.
THROUGHPUT_METRIC = "rows_checkpointed"

# Findings severities, most severe first.
SEV_CRIT = "crit"
SEV_WARN = "warn"
SEV_INFO = "info"
_SEV_ORDER = {SEV_CRIT: 0, SEV_WARN: 1, SEV_INFO: 2}

TERMINAL_STATUSES = {"DONE", "FAILED", "CANCELLED"}

# Heuristic thresholds (seconds). Deliberately coarse — they flag "worth a
# look", not SLAs.
STUCK_PENDING_SECS = 300.0
LONG_PROVISIONING_SECS = 600.0
STALE_HEARTBEAT_SECS = 300.0

DEFAULT_KUBE_CONTEXT = "cantina-prod-k8s"
DEFAULT_NAMESPACE = "lancedb"


# ---------------------------------------------------------------------------
# Record accessors (defensive: every field is getattr'd with a default)
# ---------------------------------------------------------------------------


def status_of(record: object) -> str:
    """Normalize ``record.status`` (enum or string) to its string value."""
    s = getattr(record, "status", None)
    if s is None:
        return "-"
    return getattr(s, "value", str(s))


def phase_of(events: list | None) -> str | None:
    """The latest lifecycle phase event in ``events``, or None before any.

    Returns the raw event text (e.g. ``"Executing backfill"``) so callers can
    display it verbatim; use :func:`in_phase` to test which phase it is.
    """
    latest = None
    for event in events or []:
        text = str(event)
        if text.startswith(_PHASE_PREFIXES):
            latest = text
    return latest


def in_phase(phase: str | None, prefix: str) -> bool:
    """True when ``phase`` (from :func:`phase_of`) belongs to ``prefix``."""
    return bool(phase) and str(phase).startswith(prefix)


def failure_reason(record: object) -> str | None:
    """The last ``Job failed: ...`` event, if any."""
    for event in reversed(getattr(record, "events", None) or []):
        text = str(event)
        if text.startswith("Job failed"):
            return text
    return None


def metric_value(record: object, name: str) -> tuple[int, int] | None:
    """``(n, total)`` for the named progress metric, or None if absent."""
    for m in getattr(record, "metrics", None) or []:
        if getattr(m, "name", None) == name:
            n = getattr(m, "n", 0) or 0
            total = getattr(m, "total", 0) or 0
            return int(n), int(total)
    return None


def launch_config(record: object) -> dict:
    """The job's launch ``config`` (stored as JSON text) as a dict."""
    raw = getattr(record, "config", None)
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def age_seconds(
    record: object, field: str, now: datetime | None = None
) -> float | None:
    """Seconds elapsed since ``record.<field>``, or None when unset."""
    value = getattr(record, field, None)
    if not isinstance(value, datetime):
        return None
    now = now or datetime.now(UTC)
    return max(0.0, (now - value).total_seconds())


# ---------------------------------------------------------------------------
# Throughput
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Sample:
    """A point-in-time reading of one progress counter."""

    n: int
    at: datetime


def sample(
    record: object, name: str = THROUGHPUT_METRIC, at: datetime | None = None
) -> Sample:
    """Read the named metric off ``record`` as a :class:`Sample`.

    ``at`` defaults to the record's ``updated_at`` (right for replay
    snapshots) and falls back to now (right for live polls).
    """
    value = metric_value(record, name)
    n = value[0] if value else 0
    if at is None:
        at = getattr(record, "updated_at", None)
    if not isinstance(at, datetime):
        at = datetime.now(UTC)
    return Sample(n=n, at=at)


def rate_per_second(before: Sample, after: Sample) -> float | None:
    """Counter rate between two samples; None when the window is empty."""
    secs = (after.at - before.at).total_seconds()
    if secs <= 0:
        return None
    return max(0, after.n - before.n) / secs


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One diagnostic conclusion: what was seen, what it means, what to do."""

    severity: str
    signal: str
    diagnosis: str
    action: str


def _sorted(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: _SEV_ORDER.get(f.severity, 9))


def diagnose(
    record: object,
    *,
    rate: float | None = None,
    error_count: int | None = None,
    now: datetime | None = None,
) -> list[Finding]:
    """Apply the guide's bottleneck/failure heuristics to one job record.

    ``rate`` is the observed :data:`THROUGHPUT_METRIC` rows/sec (None =
    unmeasured, 0.0 = measured and flat). ``error_count`` is the number of
    ``geneva_errors`` records for this job (None = not fetched).
    """
    findings: list[Finding] = []
    status = status_of(record)
    phase = phase_of(getattr(record, "events", None))

    if status == "FAILED":
        reason = failure_reason(record) or "no failure event recorded"
        findings.append(
            Finding(
                SEV_CRIT,
                f"job FAILED — {reason}",
                "job-level failure (not a per-row skip)",
                "read the driver pod log (`debug logs`) and per-row "
                "tracebacks (`debug errors <job-id> --trace`)",
            )
        )
    elif status == "CANCELLED":
        findings.append(
            Finding(
                SEV_INFO,
                "job marked CANCELLED",
                "cancel flips the record only — in-flight Ray tasks may have "
                "kept running until they finished or timed out",
                "check the Ray dashboard if the cluster still looks busy",
            )
        )
    elif status == "PENDING":
        pending_for = age_seconds(record, "launched_at", now)
        if pending_for is not None and pending_for > STUCK_PENDING_SECS:
            findings.append(
                Finding(
                    SEV_WARN,
                    f"PENDING for {pending_for / 60:.0f} min",
                    "the job was recorded but no driver has picked it up",
                    "look for its ray-geneva driver pod (`debug logs`); if "
                    "none exists the dispatch failed before provisioning",
                )
            )
    elif status == "RUNNING":
        findings.extend(_diagnose_running(record, phase, rate, now))

    if error_count:
        if status == "DONE":
            findings.append(
                Finding(
                    SEV_WARN,
                    f"DONE but {error_count} row(s) were skipped",
                    "the UDF's skip_on_error policy nulled failing rows; the "
                    "output column has NULLs where they failed",
                    "inspect `debug errors <job-id>`, then re-run only the "
                    "failed rows: backfill(col, where='_rowaddr IN (...)')",
                )
            )
        else:
            findings.append(
                Finding(
                    SEV_WARN,
                    f"{error_count} per-row error(s) recorded so far",
                    "rows are failing and being skipped (skip_on_error); "
                    "crossing the skip threshold will fail the job",
                    "inspect `debug errors <job-id> --trace` for the "
                    "exception; fix the UDF or the bad input rows",
                )
            )
    elif status == "DONE":
        findings.append(
            Finding(
                SEV_INFO,
                "job DONE with no per-row errors recorded",
                "healthy run",
                "spot-check output completeness with "
                "count_rows('<col> IS NULL') if you expected full coverage",
            )
        )

    return _sorted(findings)


def _diagnose_running(
    record: object,
    phase: str | None,
    rate: float | None,
    now: datetime | None,
) -> list[Finding]:
    """RUNNING-only heuristics: provisioning, worker fit, stalls, heartbeat."""
    findings: list[Finding] = []
    running_for = age_seconds(record, "launched_at", now)

    if (
        (phase is None or in_phase(phase, PHASE_PROVISIONING))
        and running_for is not None
        and running_for > LONG_PROVISIONING_SECS
    ):
        findings.append(
            Finding(
                SEV_WARN,
                f"still provisioning after {running_for / 60:.0f} min",
                "autoscaler node spin-up, image pull, or the manifest's pip "
                "install on every fresh worker",
                "keep manifests lean and reuse them across runs; watch node "
                "scale-up with `ray status` (`debug logs` has the command)",
            )
        )

    workers = metric_value(record, "workers")
    requested = workers[1] if workers else 0
    if not requested:
        cfg = launch_config(record)
        try:
            requested = int(cfg.get("concurrency") or 0)
        except (TypeError, ValueError):
            requested = 0
    if (
        workers
        and requested
        and workers[0] < requested
        and in_phase(phase, PHASE_EXECUTING)
    ):
        findings.append(
            Finding(
                SEV_WARN,
                f"only {workers[0]}/{requested} workers are up",
                "the cluster cannot schedule the per-actor ask "
                "(num_gpus/memory times concurrency does not fit)",
                "lower the UDF's num_gpus/memory ask or the backfill "
                "concurrency; `ray status` shows pending demands",
            )
        )

    heartbeat = age_seconds(record, "updated_at", now)
    if heartbeat is not None and heartbeat > STALE_HEARTBEAT_SECS:
        findings.append(
            Finding(
                SEV_WARN,
                f"no record update for {heartbeat / 60:.0f} min",
                "a RUNNING job should update its record as work progresses; "
                "a stale heartbeat suggests a wedged or dead driver",
                "check the driver pod log; if the driver is gone, re-running "
                "the backfill resumes from the last checkpoint",
            )
        )

    if rate == 0.0 and in_phase(phase, PHASE_EXECUTING):
        findings.append(
            Finding(
                SEV_WARN,
                "throughput is 0 rows/s while executing",
                "appliers are stalled — commonly OOM-restarting actors, a "
                "model reloading every batch, or blocked input I/O",
                "open the Ray dashboard Actors tab (restarts/pending) and "
                "the worker logs; check the batch-size/memory knobs",
            )
        )

    if (
        in_phase(phase, PHASE_PLANNING)
        and running_for is not None
        and running_for > LONG_PROVISIONING_SECS
    ):
        findings.append(
            Finding(
                SEV_INFO,
                "long planning phase",
                "planning scans fragments and prior checkpoints; it grows "
                "with fragment count and checkpoint history",
                "expected on large tables — nothing to fix unless it dominates the run",
            )
        )

    return findings


# ---------------------------------------------------------------------------
# Error-store summaries
# ---------------------------------------------------------------------------


def summarize_errors(errors: list) -> list[tuple[str, int, str]]:
    """Group error records: ``(error_type, count, sample_message)`` by count."""
    counts: dict[str, int] = {}
    samples: dict[str, str] = {}
    for e in errors or []:
        etype = str(getattr(e, "error_type", None) or "UnknownError")
        counts[etype] = counts.get(etype, 0) + 1
        if etype not in samples:
            samples[etype] = str(getattr(e, "error_message", "") or "")
    return [
        (etype, count, samples[etype])
        for etype, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


# ---------------------------------------------------------------------------
# Log-surface commands (the guide's three layers)
# ---------------------------------------------------------------------------


def log_commands(
    *,
    kube_context: str = DEFAULT_KUBE_CONTEXT,
    namespace: str = DEFAULT_NAMESPACE,
) -> list[tuple[str, str]]:
    """``(title, command)`` pairs for retrieving driver and worker logs."""
    kc = f"kubectl --context {kube_context} -n {namespace}"
    return [
        (
            "driver pods — job-level failures: manifest installs, admission, commits",
            f"{kc} get pods --sort-by=.metadata.creationTimestamp"
            " | grep ray-geneva | tail -5",
        ),
        (
            "follow one driver pod",
            f"{kc} logs -f <ray-geneva-...-pod>",
        ),
        (
            "Ray dashboard — per-actor worker logs, CPU/GPU utilization",
            f"{kc} port-forward svc/raycluster-head-svc 8265:8265"
            "  # then open http://localhost:8265",
        ),
        (
            "ray CLI — list submissions (find yours by table/column)",
            "RAY_ADDRESS=http://localhost:8265 ray job list",
        ),
        (
            "ray CLI — follow worker logs",
            "RAY_ADDRESS=http://localhost:8265 ray job logs <submission-id> --follow",
        ),
        (
            "cluster capacity — pending actors, autoscaler state",
            f'{kc} exec -it "$({kc} get pod -l ray.io/node-type=head -o name)"'
            " -- ray status",
        ),
    ]


# ---------------------------------------------------------------------------
# Replay snapshots (offline demo mode)
# ---------------------------------------------------------------------------

_DT_FIELDS = ("launched_at", "updated_at", "completed_at")


def record_from_dict(data: dict) -> SimpleNamespace:
    """Build a record-like object from one replay-snapshot dict.

    ISO timestamps become aware datetimes; ``metrics`` and ``errors`` entries
    become attribute objects, mirroring how geneva's ``JobRecord`` reads.
    """
    out: dict[str, Any] = dict(data)
    for field in _DT_FIELDS:
        value = out.get(field)
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            out[field] = parsed
    out["metrics"] = [
        SimpleNamespace(**m) for m in out.get("metrics") or [] if isinstance(m, dict)
    ]
    out["errors"] = [
        SimpleNamespace(**e) for e in out.get("errors") or [] if isinstance(e, dict)
    ]
    return SimpleNamespace(**out)


def load_replay(path: str) -> list[SimpleNamespace]:
    """Load a JSONL replay file: one job-record snapshot per line."""
    snapshots: list[SimpleNamespace] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                snapshots.append(record_from_dict(json.loads(line)))
    if not snapshots:
        raise ValueError(f"no snapshots in replay file: {path}")
    return snapshots


class ReplaySource:
    """Yields successive snapshots per poll, holding at the last one."""

    def __init__(self, snapshots: list) -> None:
        if not snapshots:
            raise ValueError("empty replay")
        self._snapshots = list(snapshots)
        self._i = 0

    @property
    def job_id(self) -> str:
        return str(getattr(self._snapshots[0], "job_id", "replay"))

    def fetch(self) -> object:
        record = self._snapshots[min(self._i, len(self._snapshots) - 1)]
        self._i += 1
        return record

    def errors(self) -> list:
        index = min(max(self._i - 1, 0), len(self._snapshots) - 1)
        return list(getattr(self._snapshots[index], "errors", None) or [])

    def record_now(self, record: object) -> datetime | None:
        """Replay time flows from the snapshots, not the wall clock."""
        at = getattr(record, "updated_at", None)
        return at if isinstance(at, datetime) else None


class LiveSource:
    """Polls the ``geneva_jobs`` record (and error store) over a connection."""

    def __init__(self, conn: Any, job_id: str) -> None:
        self._conn = conn
        self.job_id = job_id
        self._table_name: str | None = None

    def fetch(self) -> object:
        record = self._conn.get_job(self.job_id)
        self._table_name = getattr(record, "table_name", None) or self._table_name
        return record

    def errors(self) -> list:
        if not self._table_name:
            return []
        try:
            table = self._conn.open_table(self._table_name)
            return list(table.get_errors(job_id=self.job_id))
        except Exception:  # noqa: BLE001 — error store is best-effort
            return []

    def record_now(self, record: object) -> datetime | None:
        """Live records are judged against the wall clock."""
        return None
