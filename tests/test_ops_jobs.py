"""Tests for ops/jobs formatting helpers and job listing."""

from __future__ import annotations

import logging
import types
from datetime import UTC, datetime, timedelta

from typer.testing import CliRunner

from geneva_examples.ops import jobs


class _Status:
    def __init__(self, value):
        self.value = value


class _Metric:
    def __init__(self, name, n, total, desc=""):
        self.name, self.n, self.total, self.desc = name, n, total, desc


class _Job:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_status_reads_enum_value_or_str():
    assert jobs._status(_Job(status=_Status("RUNNING"))) == "RUNNING"
    assert jobs._status(_Job(status="DONE")) == "DONE"


def test_fmt_dt():
    dt = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert jobs._fmt_dt(dt) == "2026-01-02 03:04:05"
    assert jobs._fmt_dt(None) == "-"
    assert jobs._fmt_dt("not a date") == "-"


def test_elapsed_with_both_timestamps():
    start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    end = start + timedelta(hours=1, minutes=2, seconds=3)
    assert jobs._elapsed(_Job(launched_at=start, completed_at=end)) == "1:02:03"


def test_elapsed_uses_now_when_incomplete():
    start = datetime.now(UTC) - timedelta(seconds=65)
    out = jobs._elapsed(_Job(launched_at=start, completed_at=None))
    assert out.startswith("0:01:")


def test_elapsed_missing_start():
    assert jobs._elapsed(_Job(launched_at=None)) == "-"


def test_metrics_line():
    job = _Job(metrics=[_Metric("rows", 3, 10), _Metric("bytes", 1, 2)])
    assert jobs._metrics_line(job) == "rows 3/10  bytes 1/2"
    assert jobs._metrics_line(_Job(metrics=None)) == ""


def test_fmt_config_variants():
    assert jobs._fmt_config(None) == ""
    assert jobs._fmt_config('{"a": 1}') == '{\n  "a": 1\n}'
    assert jobs._fmt_config("not json") == "not json"
    assert "b" in jobs._fmt_config({"b": 2})


class _Conn:
    def __init__(self, by_status, failing=()):
        self._by_status = by_status
        self._failing = set(failing)

    def list_jobs(self, table_name, status):
        if status in self._failing:
            raise RuntimeError("backend error")
        return self._by_status.get(status, [])


def test_list_jobs_merges_and_dedupes_by_id():
    a = _Job(job_id="1")
    b = _Job(job_id="2")
    conn = _Conn({"RUNNING": [a, b], "PENDING": [a]})  # 'a' appears twice
    merged = jobs._list_jobs(conn, None, ["RUNNING", "PENDING"])
    ids = sorted(j.job_id for j in merged)
    assert ids == ["1", "2"]


def test_list_jobs_tolerates_failing_status(caplog):
    conn = _Conn({"DONE": [_Job(job_id="9")]}, failing=["RUNNING"])
    with caplog.at_level(logging.WARNING):
        merged = jobs._list_jobs(conn, None, ["RUNNING", "DONE"])
    assert [j.job_id for j in merged] == ["9"]
    assert "list_jobs(status=RUNNING) failed" in caplog.text


def test_print_detail_smoke(capsys):
    job = _Job(
        job_id="abc",
        status=_Status("RUNNING"),
        job_type="BACKFILL",
        table_name="video_clips",
        column_name="embedding",
        launched_at=datetime(2026, 1, 1, tzinfo=UTC),
        metrics=[_Metric("rows", 1, 2, "progress")],
        events=["started", "tick", "done"],
    )
    jobs._print_detail(job, events_limit=2)
    out = capsys.readouterr().out
    assert "job_id:     abc" in out
    assert "video_clips.embedding" in out
    assert "events (3 total" in out  # 3 events, last 2 shown


class _History:
    """Stand-in for geneva's private jobs-history table."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def set_completed(self, job_id, status):
        self.calls.append((job_id, status))


class _KillConn:
    def __init__(self, job, with_history=True):
        self._job = job
        if with_history:
            self._history = _History()

    def get_job(self, job_id):
        if self._job is None or job_id != self._job.job_id:
            raise ValueError("not found")
        return self._job


def _patch_kill(monkeypatch, conn):
    monkeypatch.setattr(
        jobs, "load_config", lambda _c, **_kw: types.SimpleNamespace(db_uri="db://test")
    )
    monkeypatch.setattr(jobs, "connect", lambda _cfg: conn)


def _running_job(job_id="j1"):
    return _Job(
        job_id=job_id,
        status=_Status("RUNNING"),
        table_name="images",
        column_name="embedding",
        launched_at=datetime(2026, 1, 1, tzinfo=UTC),
        events=[],
    )


def test_kill_marks_running_job_cancelled(monkeypatch, fake_geneva):
    conn = _KillConn(_running_job())
    _patch_kill(monkeypatch, conn)

    result = CliRunner().invoke(jobs.app, ["kill", "j1", "--yes"])

    assert result.exit_code == 0, result.output
    assert "marked job j1 CANCELLED" in result.output
    assert conn._history.calls == [("j1", "CANCELLED")]


def test_kill_missing_job_errors(monkeypatch, fake_geneva):
    _patch_kill(monkeypatch, _KillConn(None))

    result = CliRunner().invoke(jobs.app, ["kill", "ghost", "--yes"])

    assert result.exit_code == 1
    assert "not found" in result.output


def test_kill_already_terminal_is_noop(monkeypatch, fake_geneva):
    job = _running_job()
    job.status = _Status("DONE")
    conn = _KillConn(job)
    _patch_kill(monkeypatch, conn)

    result = CliRunner().invoke(jobs.app, ["kill", "j1", "--yes"])

    assert result.exit_code == 0, result.output
    assert "already DONE" in result.output
    assert conn._history.calls == []  # not cancelled


def test_kill_guards_missing_history_api(monkeypatch, fake_geneva):
    # A geneva build without conn._history.set_completed -> clear error, exit 1.
    conn = _KillConn(_running_job(), with_history=False)
    _patch_kill(monkeypatch, conn)

    result = CliRunner().invoke(jobs.app, ["kill", "j1", "--yes"])

    assert result.exit_code == 1
    assert "does not expose the private jobs-history API" in result.output
