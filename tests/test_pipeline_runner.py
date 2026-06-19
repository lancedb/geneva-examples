"""Tests for the shared stage backfill orchestration."""

from __future__ import annotations

from datetime import timedelta

from geneva_examples.pipeline.stages import _runner


class _Job:
    job_id = "job-123"


class _Table:
    def __init__(self, names, *, drop_raises=False):
        self.schema = type("S", (), {"names": names})()
        self.calls: dict = {}
        self._drop_raises = drop_raises

    def drop_columns(self, cols):
        if self._drop_raises:
            raise RuntimeError("column does not exist")
        self.calls["drop"] = cols

    def add_columns(self, mapping):
        self.calls["add"] = mapping

    def backfill(self, column, **kw):
        self.calls["backfill"] = {"column": column, **kw}
        return _Job()

    def checkout_latest(self):
        self.calls["checkout"] = True

    def count_rows(self, where=None):
        self.calls["count_where"] = where
        return 0


class _Conn:
    def __init__(self, table):
        self._table = table

    def open_table(self, _name):
        return self._table


def _run(table):
    sentinel_udf = object()
    return _runner.backfill_column(
        conn=_Conn(table),
        table=table,
        table_name="video_clips",
        column="embedding",
        udf=sentinel_udf,
        concurrency=16,
        task_size=4096,
        checkpoint_size=4096,
        flush_interval_s=2.0,
        timeout_min=30,
        wait_attempts=3,
        wait_sleep_s=0,
    ), sentinel_udf


def test_backfill_column_happy_path():
    table = _Table(["video_id", "embedding"])
    returned, sentinel_udf = _run(table)
    assert returned is table
    assert table.calls["drop"] == ["embedding"]
    assert table.calls["add"] == {"embedding": sentinel_udf}
    bf = table.calls["backfill"]
    assert bf["column"] == "embedding"
    assert bf["concurrency"] == 16
    assert bf["task_size"] == 4096
    assert bf["checkpoint_size"] == 4096
    assert bf["batch_checkpoint_flush_interval_seconds"] == 2.0
    assert bf["use_cpu_only_pool"] is False
    assert bf["timeout"] == timedelta(minutes=30)
    assert table.calls["checkout"] is True
    assert table.calls["count_where"] == "`embedding` IS NULL"


def test_backfill_column_tolerates_missing_column_on_drop():
    table = _Table(["video_id", "embedding"], drop_raises=True)
    _run(table)
    # drop raised and was swallowed; the rest still ran.
    assert "drop" not in table.calls
    assert "add" in table.calls
    assert "backfill" in table.calls
