"""Tests for the pure job-diagnosis logic behind the ``debug`` CLI/TUI."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from geneva_examples.core import diagnose as dx

NOW = datetime(2026, 7, 23, 18, 30, 0, tzinfo=UTC)


class _Status:
    def __init__(self, value):
        self.value = value


class _Metric:
    def __init__(self, name, n, total, desc=""):
        self.name, self.n, self.total, self.desc = name, n, total, desc


class _Job:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _running(minutes_ago=1.0, **kw):
    launched = NOW - timedelta(minutes=minutes_ago)
    defaults = dict(
        status="RUNNING",
        launched_at=launched,
        updated_at=NOW,
        events=["Cluster provisioning", "Job planning", "Executing backfill"],
        metrics=[],
        config="{}",
    )
    defaults.update(kw)
    return _Job(**defaults)


# ---------------------------------------------------------------------------
# accessors
# ---------------------------------------------------------------------------


def test_status_of_enum_str_and_missing():
    assert dx.status_of(_Job(status=_Status("RUNNING"))) == "RUNNING"
    assert dx.status_of(_Job(status="DONE")) == "DONE"
    assert dx.status_of(_Job(status=None)) == "-"
    assert dx.status_of(_Job()) == "-"


def test_phase_of_returns_latest_phase_event():
    events = [
        "Cluster provisioning",
        "Job planning",
        "Executing backfill",
        "Job completed with status DONE",
    ]
    assert dx.phase_of(events) == "Executing backfill"
    assert dx.phase_of(["Cluster provisioning"]) == "Cluster provisioning"
    assert dx.phase_of([]) is None
    assert dx.phase_of(None) is None


def test_in_phase():
    assert dx.in_phase("Executing backfill", dx.PHASE_EXECUTING)
    assert not dx.in_phase("Job planning", dx.PHASE_EXECUTING)
    assert not dx.in_phase(None, dx.PHASE_EXECUTING)


def test_failure_reason_picks_last_failure_event():
    record = _Job(events=["Executing backfill", "Job failed: boom"])
    assert dx.failure_reason(record) == "Job failed: boom"
    assert dx.failure_reason(_Job(events=["Executing backfill"])) is None
    assert dx.failure_reason(_Job(events=None)) is None


def test_metric_value():
    record = _Job(metrics=[_Metric("workers", 3, 8), _Metric("rows", None, None)])
    assert dx.metric_value(record, "workers") == (3, 8)
    assert dx.metric_value(record, "rows") == (0, 0)  # None coerces to 0
    assert dx.metric_value(record, "absent") is None
    assert dx.metric_value(_Job(metrics=None), "workers") is None


def test_launch_config_variants():
    assert dx.launch_config(_Job(config='{"concurrency": 8}')) == {"concurrency": 8}
    assert dx.launch_config(_Job(config={"a": 1})) == {"a": 1}
    assert dx.launch_config(_Job(config="not json")) == {}
    assert dx.launch_config(_Job(config="[1, 2]")) == {}  # non-dict JSON
    assert dx.launch_config(_Job(config=None)) == {}
    assert dx.launch_config(_Job()) == {}


def test_age_seconds():
    record = _Job(launched_at=NOW - timedelta(seconds=90))
    assert dx.age_seconds(record, "launched_at", now=NOW) == 90.0
    assert dx.age_seconds(_Job(launched_at=None), "launched_at", now=NOW) is None
    assert dx.age_seconds(_Job(), "launched_at", now=NOW) is None
    # clamped at zero when the field is in the future
    future = _Job(launched_at=NOW + timedelta(seconds=5))
    assert dx.age_seconds(future, "launched_at", now=NOW) == 0.0


# ---------------------------------------------------------------------------
# throughput
# ---------------------------------------------------------------------------


def test_sample_prefers_updated_at_then_now():
    record = _Job(metrics=[_Metric(dx.THROUGHPUT_METRIC, 42, 100)], updated_at=NOW)
    s = dx.sample(record)
    assert (s.n, s.at) == (42, NOW)

    override = NOW + timedelta(seconds=5)
    assert dx.sample(record, at=override).at == override

    no_ts = dx.sample(_Job(metrics=[], updated_at=None))
    assert no_ts.n == 0
    assert no_ts.at.tzinfo is not None  # fell back to a real "now"


def test_rate_per_second():
    a = dx.Sample(n=100, at=NOW)
    b = dx.Sample(n=160, at=NOW + timedelta(seconds=30))
    assert dx.rate_per_second(a, b) == pytest.approx(2.0)
    assert dx.rate_per_second(a, dx.Sample(n=200, at=NOW)) is None  # empty window
    # counter reset clamps to zero instead of going negative
    assert dx.rate_per_second(b, dx.Sample(n=0, at=NOW + timedelta(seconds=60))) == 0.0


# ---------------------------------------------------------------------------
# findings
# ---------------------------------------------------------------------------


def _severities(findings):
    return [f.severity for f in findings]


def test_diagnose_failed_is_crit_and_sorts_first():
    record = _running(status="FAILED", events=["Executing backfill", "Job failed: oom"])
    findings = dx.diagnose(record, error_count=3, now=NOW)
    assert findings[0].severity == dx.SEV_CRIT
    assert "Job failed: oom" in findings[0].signal
    assert dx.SEV_WARN in _severities(findings)  # the per-row errors finding


def test_diagnose_failed_without_failure_event():
    record = _running(status="FAILED", events=["Executing backfill"])
    findings = dx.diagnose(record, now=NOW)
    assert "no failure event recorded" in findings[0].signal


def test_diagnose_cancelled_mentions_kill_semantics():
    findings = dx.diagnose(_running(status="CANCELLED"), now=NOW)
    assert findings[0].severity == dx.SEV_INFO
    assert "in-flight" in findings[0].diagnosis


def test_diagnose_pending():
    fresh = _running(status="PENDING", minutes_ago=1, events=[])
    assert dx.diagnose(fresh, now=NOW) == []
    stuck = _running(status="PENDING", minutes_ago=10, events=[])
    findings = dx.diagnose(stuck, now=NOW)
    assert findings and findings[0].severity == dx.SEV_WARN
    assert "PENDING" in findings[0].signal


def test_diagnose_long_provisioning():
    record = _running(minutes_ago=15, events=["Cluster provisioning"])
    findings = dx.diagnose(record, now=NOW)
    assert any("provisioning" in f.signal for f in findings)
    # also fires before any phase event exists
    record = _running(minutes_ago=15, events=[])
    assert any("provisioning" in f.signal for f in dx.diagnose(record, now=NOW))
    # but not while executing
    record = _running(minutes_ago=15)
    assert not any("provisioning" in f.signal for f in dx.diagnose(record, now=NOW))


def test_diagnose_workers_below_request():
    record = _running(metrics=[_Metric("workers", 2, 8)])
    findings = dx.diagnose(record, now=NOW)
    assert any("2/8 workers" in f.signal for f in findings)


def test_diagnose_workers_request_falls_back_to_config():
    record = _running(
        metrics=[_Metric("workers", 2, 0)], config=json.dumps({"concurrency": 8})
    )
    assert any("2/8" in f.signal for f in dx.diagnose(record, now=NOW))
    # bad config value is ignored rather than raising
    record = _running(
        metrics=[_Metric("workers", 2, 0)], config=json.dumps({"concurrency": "x"})
    )
    assert not any("workers" in f.signal for f in dx.diagnose(record, now=NOW))


def test_diagnose_workers_quiet_when_full_or_not_executing():
    record = _running(metrics=[_Metric("workers", 8, 8)])
    assert not any("workers" in f.signal for f in dx.diagnose(record, now=NOW))
    provisioning = _running(
        events=["Cluster provisioning"], metrics=[_Metric("workers", 2, 8)]
    )
    assert not any("workers" in f.signal for f in dx.diagnose(provisioning, now=NOW))


def test_diagnose_stale_heartbeat():
    record = _running(updated_at=NOW - timedelta(minutes=10))
    findings = dx.diagnose(record, now=NOW)
    assert any("no record update" in f.signal for f in findings)


def test_diagnose_stalled_throughput_only_when_measured_zero():
    record = _running()
    assert any("0 rows/s" in f.signal for f in dx.diagnose(record, rate=0.0, now=NOW))
    assert not any(
        "0 rows/s" in f.signal for f in dx.diagnose(record, rate=None, now=NOW)
    )
    assert not any(
        "0 rows/s" in f.signal for f in dx.diagnose(record, rate=3.5, now=NOW)
    )


def test_diagnose_long_planning_is_info():
    record = _running(minutes_ago=15, events=["Cluster provisioning", "Job planning"])
    findings = dx.diagnose(record, now=NOW)
    assert any(f.severity == dx.SEV_INFO and "planning" in f.signal for f in findings)


def test_diagnose_done():
    clean = dx.diagnose(_running(status="DONE"), now=NOW)
    assert clean and clean[0].severity == dx.SEV_INFO
    skipped = dx.diagnose(_running(status="DONE"), error_count=4, now=NOW)
    assert skipped[0].severity == dx.SEV_WARN
    assert "4 row(s) were skipped" in skipped[0].signal
    assert "_rowaddr" in skipped[0].action


def test_diagnose_running_errors_accumulating():
    findings = dx.diagnose(_running(), error_count=2, now=NOW)
    assert any("2 per-row error(s)" in f.signal for f in findings)


# ---------------------------------------------------------------------------
# error summaries / log commands
# ---------------------------------------------------------------------------


def test_summarize_errors_groups_and_sorts():
    errors = [
        _Job(error_type="OOM", error_message="gpu"),
        _Job(error_type="ValueError", error_message="bad row"),
        _Job(error_type="OOM", error_message="ignored second sample"),
        _Job(error_type=None, error_message=None),
    ]
    summary = dx.summarize_errors(errors)
    assert summary[0] == ("OOM", 2, "gpu")
    assert ("ValueError", 1, "bad row") in summary
    assert ("UnknownError", 1, "") in summary
    assert dx.summarize_errors([]) == []


def test_log_commands_cover_the_three_surfaces():
    commands = dict(dx.log_commands())
    joined = "\n".join(commands.values())
    assert "grep ray-geneva" in joined
    assert "port-forward svc/raycluster-head-svc 8265:8265" in joined
    assert "ray job logs" in joined
    assert "ray status" in joined
    assert f"--context {dx.DEFAULT_KUBE_CONTEXT}" in joined
    custom = dx.log_commands(kube_context="kind-dev", namespace="geneva")
    assert all("--context kind-dev -n geneva" in c for _, c in custom if "kubectl" in c)


# ---------------------------------------------------------------------------
# replay snapshots and sources
# ---------------------------------------------------------------------------


def _snapshot_dict(status="RUNNING", n=10):
    return {
        "job_id": "j1",
        "status": status,
        "launched_at": "2026-07-23T18:00:00+00:00",
        "updated_at": "2026-07-23T18:05:00",  # naive: should gain UTC
        "completed_at": None,
        "events": ["Cluster provisioning"],
        "metrics": [{"name": dx.THROUGHPUT_METRIC, "n": n, "total": 100}],
        "errors": [{"error_type": "OOM", "error_message": "gpu"}],
    }


def test_record_from_dict_parses_timestamps_and_nests():
    record = dx.record_from_dict(_snapshot_dict())
    assert record.launched_at.tzinfo is not None
    assert record.updated_at.tzinfo is UTC
    assert record.completed_at is None
    assert record.metrics[0].name == dx.THROUGHPUT_METRIC
    assert record.errors[0].error_type == "OOM"


def test_load_replay_round_trip(tmp_path):
    path = tmp_path / "replay.jsonl"
    lines = [json.dumps(_snapshot_dict(n=n)) for n in (0, 50)]
    path.write_text("\n".join(lines) + "\n\n")  # trailing blank line ignored
    snapshots = dx.load_replay(str(path))
    assert [dx.metric_value(s, dx.THROUGHPUT_METRIC)[0] for s in snapshots] == [0, 50]

    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n")
    with pytest.raises(ValueError, match="no snapshots"):
        dx.load_replay(str(empty))


def test_replay_source_advances_then_holds():
    snapshots = [dx.record_from_dict(_snapshot_dict(n=n)) for n in (0, 5, 9)]
    source = dx.ReplaySource(snapshots)
    assert source.job_id == "j1"
    seen = [dx.metric_value(source.fetch(), dx.THROUGHPUT_METRIC)[0] for _ in range(5)]
    assert seen == [0, 5, 9, 9, 9]
    assert source.errors()[0].error_type == "OOM"
    assert source.record_now(snapshots[0]) == snapshots[0].updated_at
    with pytest.raises(ValueError):
        dx.ReplaySource([])


class _FakeErrTable:
    def __init__(self, errors=(), raises=False):
        self._errors, self._raises = list(errors), raises

    def get_errors(self, job_id):
        if self._raises:
            raise RuntimeError("no error store")
        return self._errors


class _FakeConn:
    def __init__(self, record, table):
        self._record, self._table = record, table

    def get_job(self, job_id):
        return self._record

    def open_table(self, name):
        return self._table


def test_live_source_fetch_then_errors():
    record = _Job(status="RUNNING", table_name="video_clips")
    err = _Job(error_type="OOM")
    source = dx.LiveSource(_FakeConn(record, _FakeErrTable([err])), "j1")
    assert source.errors() == []  # table unknown until first fetch
    assert source.fetch() is record
    assert source.errors() == [err]
    assert source.record_now(record) is None


def test_live_source_errors_swallow_backend_failure():
    record = _Job(status="RUNNING", table_name="t")
    source = dx.LiveSource(_FakeConn(record, _FakeErrTable(raises=True)), "j1")
    source.fetch()
    assert source.errors() == []
