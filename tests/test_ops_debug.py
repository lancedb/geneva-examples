"""CliRunner tests for the ``debug`` ops CLI (report / errors / logs)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from geneva_examples.ops import debug

runner = CliRunner()

DEMO = Path(__file__).resolve().parent.parent / "demo_data"
HEALTHY = str(DEMO / "debug_healthy_run.jsonl")
STUCK = str(DEMO / "debug_stuck_workers.jsonl")


class _Metric:
    def __init__(self, name, n, total, desc=""):
        self.name, self.n, self.total, self.desc = name, n, total, desc


class _Job:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeErrTable:
    def __init__(self, errors=()):
        self._errors = list(errors)

    def get_errors(self, job_id):
        return self._errors


class _FakeConn:
    def __init__(self, record=None, table=None):
        self._record, self._table = record, table

    def get_job(self, job_id):
        if self._record is None:
            raise ValueError(f"Job {job_id} not found")
        return self._record

    def open_table(self, name):
        return self._table


def _patch_conn(monkeypatch, conn):
    monkeypatch.setattr(debug, "_open_connection", lambda *a, **kw: (None, conn))


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def test_report_replay_healthy():
    result = runner.invoke(debug.app, ["report", "--replay", HEALTHY])
    assert result.exit_code == 0
    assert "status:     DONE" in result.output
    assert "throughput: ~" in result.output
    assert "healthy run" in result.output
    assert "NEXT STEPS" in result.output


def test_report_replay_stuck_diagnoses_failure():
    result = runner.invoke(debug.app, ["report", "--replay", STUCK])
    assert result.exit_code == 0
    assert "status:     FAILED" in result.output
    assert "[CRIT]" in result.output
    assert "OutOfMemoryError x3" in result.output
    assert "grep ray-geneva" in result.output
    assert "--context cantina-prod-k8s" in result.output


def test_report_requires_job_id_or_replay():
    result = runner.invoke(debug.app, ["report"])
    assert result.exit_code == 2
    assert "JOB_ID" in result.output


def test_report_live_running_job(monkeypatch):
    record = _Job(
        job_id="j-live",
        job_type="BACKFILL",
        table_name="images",
        column_name="embedding",
        status="RUNNING",
        launched_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        launched_by="me",
        events=["Cluster provisioning", "Job planning", "Executing backfill"],
        metrics=[_Metric("workers", 2, 8)],
        config='{"concurrency": 8}',
    )
    _patch_conn(monkeypatch, _FakeConn(record, _FakeErrTable()))
    result = runner.invoke(debug.app, ["report", "j-live", "--sample-secs", "0"])
    assert result.exit_code == 0
    assert "status:     RUNNING" in result.output
    assert "2/8 workers" in result.output  # the fit heuristic fired
    assert "throughput" not in result.output  # sampling skipped


def test_report_defaults_to_local_mode(monkeypatch):
    seen = {}

    def spy(config, db_uri, log_level, mode=None):
        seen["mode"] = mode
        record = _Job(job_id="j", table_name="t", column_name="c", status="DONE")
        return None, _FakeConn(record, _FakeErrTable())

    monkeypatch.setattr(debug, "_open_connection", spy)
    result = runner.invoke(debug.app, ["report", "j", "--sample-secs", "0"])
    assert result.exit_code == 0
    assert seen["mode"] == "local"

    result = runner.invoke(
        debug.app, ["report", "j", "--sample-secs", "0", "--mode", "enterprise"]
    )
    assert result.exit_code == 0
    assert seen["mode"] == "enterprise"


def test_report_live_unknown_job(monkeypatch):
    _patch_conn(monkeypatch, _FakeConn(record=None))
    result = runner.invoke(debug.app, ["report", "nope", "--sample-secs", "0"])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_bare_invocation_prints_help():
    result = runner.invoke(debug.app, [])
    assert result.exit_code == 0
    assert "report" in result.output
    assert "watch" in result.output


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


def test_errors_replay_lists_and_traces():
    result = runner.invoke(debug.app, ["errors", "--replay", STUCK])
    assert result.exit_code == 0
    assert "3 error record(s)" in result.output
    assert "row=4230" in result.output
    assert "_rowaddr IN" in result.output

    traced = runner.invoke(debug.app, ["errors", "--replay", STUCK, "--trace"])
    assert traced.exit_code == 0
    assert "Traceback (most recent call last)" in traced.output

    out_of_range = runner.invoke(
        debug.app, ["errors", "--replay", STUCK, "--trace", "--index", "99"]
    )
    assert out_of_range.exit_code == 2


def test_errors_replay_healthy_has_none():
    result = runner.invoke(debug.app, ["errors", "--replay", HEALTHY])
    assert result.exit_code == 0
    assert "no error records" in result.output


def test_errors_live(monkeypatch):
    record = _Job(job_id="j1", table_name="t", status="DONE")
    err = _Job(
        error_type="ValueError",
        error_message="bad pixel",
        error_trace="Traceback ...",
        row_address=7,
        fragment_id=1,
        attempt=2,
    )
    _patch_conn(monkeypatch, _FakeConn(record, _FakeErrTable([err])))
    result = runner.invoke(debug.app, ["errors", "j1"])
    assert result.exit_code == 0
    assert "ValueError" in result.output
    assert "row=7" in result.output


def test_errors_requires_job_id_or_replay():
    result = runner.invoke(debug.app, ["errors"])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# logs / watch
# ---------------------------------------------------------------------------


def test_logs_lists_three_surfaces_with_overrides():
    result = runner.invoke(
        debug.app, ["logs", "--kube-context", "kind-dev", "--namespace", "geneva"]
    )
    assert result.exit_code == 0
    assert "three places" in result.output
    assert "--context kind-dev -n geneva" in result.output
    assert "ray job logs" in result.output
    assert "durable stores" in result.output


def test_watch_requires_job_id_or_replay():
    result = runner.invoke(debug.app, ["watch"])
    assert result.exit_code == 2
