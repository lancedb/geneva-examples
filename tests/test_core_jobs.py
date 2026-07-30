"""Tests for the shared job-record helpers (core/jobs.py).

These back both the ``jobs`` CLI and the TUI's Jobs view, so they are unit
tested here once rather than through either surface.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from geneva_examples.core import jobs


class _Status:
    def __init__(self, value):
        self.value = value


class _Metric:
    def __init__(self, name, n, total, desc=""):
        self.name, self.n, self.total, self.desc = name, n, total, desc


class _Job:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_job_status_reads_enum_value_or_str():
    assert jobs.job_status(_Job(status=_Status("RUNNING"))) == "RUNNING"
    assert jobs.job_status(_Job(status="DONE")) == "DONE"


def test_job_target_falls_back_when_fields_missing():
    assert jobs.job_target(_Job(table_name="images", column_name="embedding")) == (
        "images.embedding"
    )
    assert jobs.job_target(_Job()) == "-.-"


def test_format_dt():
    dt = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert jobs.format_dt(dt) == "2026-01-02 03:04:05"
    assert jobs.format_dt(None) == "-"
    assert jobs.format_dt("not a date") == "-"


def test_elapsed_with_both_timestamps():
    start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    end = start + timedelta(hours=1, minutes=2, seconds=3)
    assert jobs.elapsed(_Job(launched_at=start, completed_at=end)) == "1:02:03"


def test_elapsed_uses_now_when_incomplete():
    start = datetime.now(UTC) - timedelta(seconds=65)
    out = jobs.elapsed(_Job(launched_at=start, completed_at=None))
    assert out.startswith("0:01:")


def test_elapsed_missing_start():
    assert jobs.elapsed(_Job(launched_at=None)) == "-"


def test_metrics_line_includes_percentage():
    job = _Job(metrics=[_Metric("rows", 3, 10), _Metric("bytes", 1, 2)])
    assert jobs.metrics_line(job) == "rows 3/10 (30%)  bytes 1/2 (50%)"
    assert jobs.metrics_line(_Job(metrics=None)) == ""


def test_metrics_line_skips_percentage_when_counters_unusable():
    # geneva leaves the counters unset early on, and a 0 total would divide.
    job = _Job(metrics=[_Metric("rows", "?", "?"), _Metric("bytes", 0, 0)])
    assert jobs.metrics_line(job) == "rows ?/?  bytes 0/0"


def test_progress_summary_prefers_the_metrics_that_read_as_progress():
    job = _Job(
        metrics=[
            _Metric("plan_read_time_ms", 3, 0),  # a timer, not progress
            _Metric("rows_checkpointed", 89, 89),  # an earlier stage of the rows
            _Metric("fragments", 1, 1),
            _Metric("rows_committed", 89, 89),
            _Metric("tasks_completed", 1, 1),
        ]
    )
    assert jobs.progress_summary(job) == (
        "rows_committed 89/89 (100%)  tasks_completed 1/1 (100%)  fragments 1/1 (100%)"
    )


def test_progress_summary_falls_back_to_any_ratio_metric():
    job = _Job(metrics=[_Metric("workers", 1, 4), _Metric("elapsed_ms", 20, 0)])
    assert jobs.progress_summary(job) == "workers 1/4 (25%)"


def test_progress_summary_empty_without_ratio_metrics():
    assert jobs.progress_summary(_Job(metrics=[_Metric("udf_calls", 89, 0)])) == ""
    assert jobs.progress_summary(_Job(metrics=None)) == ""


def test_format_config_variants():
    assert jobs.format_config(None) == ""
    assert jobs.format_config('{"a": 1}') == '{\n  "a": 1\n}'
    assert jobs.format_config("not json") == "not json"
    assert "b" in jobs.format_config({"b": 2})


def test_status_counts_ranks_by_lifecycle_order():
    records = [
        _Job(status=_Status("DONE")),
        _Job(status=_Status("RUNNING")),
        _Job(status=_Status("DONE")),
    ]
    assert jobs.status_counts(records) == "1 RUNNING · 2 DONE"
    assert jobs.status_counts([]) == ""


def test_sort_newest_first_puts_undated_last():
    old = _Job(job_id="old", launched_at=datetime(2026, 1, 1, tzinfo=UTC))
    new = _Job(job_id="new", launched_at=datetime(2026, 6, 1, tzinfo=UTC))
    undated = _Job(job_id="undated", launched_at=None)
    ordered = jobs.sort_newest_first([old, undated, new])
    assert [j.job_id for j in ordered] == ["new", "old", "undated"]


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
    merged = jobs.list_jobs(conn, None, ["RUNNING", "PENDING"])
    ids = sorted(j.job_id for j in merged)
    assert ids == ["1", "2"]


def test_list_jobs_tolerates_failing_status(caplog):
    conn = _Conn({"DONE": [_Job(job_id="9")]}, failing=["RUNNING"])
    with caplog.at_level(logging.WARNING):
        merged = jobs.list_jobs(conn, None, ["RUNNING", "DONE"])
    assert [j.job_id for j in merged] == ["9"]
    assert "list_jobs(status=RUNNING) failed" in caplog.text


def _detail_job(**overrides):
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
    job.__dict__.update(overrides)
    return job


def test_format_detail_renders_record_and_truncates_events():
    out = jobs.format_detail(_detail_job(), events_limit=2)
    assert "job_id:     abc" in out
    assert "video_clips.embedding" in out
    assert "events (3 total, showing last 2)" in out
    assert "started" not in out  # oldest event dropped by the limit


def test_format_detail_unlimited_events_shows_whole_log():
    out = jobs.format_detail(_detail_job(), events_limit=None)
    assert "events (3 total)" in out
    assert "started" in out


def test_format_detail_omits_absent_optional_fields():
    out = jobs.format_detail(_detail_job(metrics=[], events=[], config=None))
    assert "manifest:" not in out
    assert "object_ref:" not in out
    assert "config:" not in out
    assert "metrics:" not in out
    assert "events" not in out


def test_format_detail_includes_optional_fields_when_present():
    out = jobs.format_detail(
        _detail_job(
            object_ref="gAWVOAAA...",
            manifest_id="mf-123",
            manifest_checksum="sha256:abc",
            config='{"where": "score IS NULL"}',
        )
    )
    assert "object_ref: gAWVOAAA..." in out
    assert "manifest:   mf-123 (checksum sha256:abc)" in out
    # config is pretty-printed and indented under a `config:` header
    assert "config:" in out
    assert '    "where": "score IS NULL"' in out
    # metrics keep their name/n/total/desc shape
    assert "    rows: 1/2 progress" in out


def test_format_detail_manifest_without_checksum_shows_dash():
    out = jobs.format_detail(_detail_job(manifest_id="mf-9", manifest_checksum=None))
    assert "manifest:   mf-9 (checksum -)" in out


def test_format_detail_falls_back_when_fields_are_missing_entirely():
    # A record from a geneva pin that dropped fields must still render.
    out = jobs.format_detail(_Job())
    assert "job_id:     -" in out
    assert "target:     -.-" in out
    assert "cluster:    -" in out
    assert "elapsed:    -" in out


def test_format_config_falls_back_to_str_when_unserializable():
    class _Boom:
        def __repr__(self):
            return "<boom>"

    # A dict whose *key* isn't sortable defeats json.dumps(sort_keys=True).
    out = jobs.format_config({_Boom(): 1, "a": 2})
    assert "boom" in out


def test_progress_summary_respects_limit():
    job = _Job(
        metrics=[
            _Metric("rows_committed", 1, 2),
            _Metric("tasks_completed", 1, 2),
            _Metric("fragments", 1, 2),
        ]
    )
    assert jobs.progress_summary(job, limit=1) == "rows_committed 1/2 (50%)"
    assert jobs.progress_summary(job, limit=2).count("  ") == 1  # two entries


def test_progress_summary_ignores_bool_counters():
    # bools are ints in Python; a True/True metric is not 100% progress.
    assert jobs.progress_summary(_Job(metrics=[_Metric("done", True, True)])) == ""


def test_status_counts_puts_unknown_statuses_last():
    records = [
        _Job(status=_Status("WEIRD")),
        _Job(status=_Status("DONE")),
        _Job(status=_Status("PENDING")),
    ]
    assert jobs.status_counts(records) == "1 PENDING · 1 DONE · 1 WEIRD"
