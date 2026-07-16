"""Tests for the shared stage backfill orchestration."""

from __future__ import annotations

from datetime import timedelta

from geneva_examples.core import backfill as _runner


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
        # Reflect the new column so wait_for_columns() sees it and returns.
        for name in mapping:
            if name not in self.schema.names:
                self.schema.names.append(name)

    def backfill(self, column, **kw):
        self.calls["backfill"] = {"column": column, **kw}
        return _Job()

    def checkout_latest(self):
        self.calls["checkout"] = True

    def count_rows(self, where=None):
        self.calls["count_where"] = where
        return 0


class _Conn:
    def __init__(self, table, *, is_remote=True):
        self._table = table
        self._is_remote = is_remote

    def open_table(self, _name):
        return self._table

    def is_remote(self):
        return self._is_remote


def _run(table, *, is_remote=True, reset=True, cluster=None):
    sentinel_udf = object()
    return _runner.backfill_column(
        conn=_Conn(table, is_remote=is_remote),
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
        reset=reset,
        cluster=cluster,
    ), sentinel_udf


def test_backfill_column_happy_path_enterprise():
    table = _Table(["video_id", "embedding"])
    returned, sentinel_udf = _run(table)
    assert returned is table
    assert table.calls["drop"] == ["embedding"]
    assert table.calls["add"] == {"embedding": sentinel_udf}
    bf = table.calls["backfill"]
    assert bf["column"] == "embedding"
    assert bf["concurrency"] == 16
    # Enterprise (remote) backfill uses the cloud kwargs.
    assert bf["task_size"] == 4096
    assert bf["checkpoint_size"] == 4096
    assert bf["use_cpu_only_pool"] is False
    assert bf["batch_checkpoint_flush_interval_seconds"] == 2.0
    assert bf["timeout"] == timedelta(minutes=30)
    assert "max_checkpoint_size" not in bf
    assert "_admission_check" not in bf
    # Remote dispatch routes to a unique per-job cluster (auto-generated from
    # table+column) so concurrent jobs don't collide under ephemeral clusters.
    assert bf["cluster"].startswith("video-clips-embedding-")
    assert table.calls["checkout"] is True
    assert table.calls["count_where"] == "`embedding` IS NULL"


def test_backfill_column_honors_explicit_cluster():
    table = _Table(["video_id", "embedding"])
    _run(table, cluster="my-fixed-cluster")
    assert table.calls["backfill"]["cluster"] == "my-fixed-cluster"


def test_backfill_column_local_uses_native_kwargs():
    table = _Table(["video_id", "embedding"])
    _run(table, is_remote=False)
    bf = table.calls["backfill"]
    # Local (NativeTable) backfill drops task_size/use_cpu_only_pool and maps
    # checkpoint_size -> max_checkpoint_size.
    assert bf["max_checkpoint_size"] == 4096
    assert bf["_admission_check"] is False
    assert "task_size" not in bf
    assert "use_cpu_only_pool" not in bf
    assert "checkpoint_size" not in bf
    # cluster override is remote-only; not passed for native backfill.
    assert "cluster" not in bf
    # Local concurrency is capped to the machine's core count.
    assert bf["concurrency"] <= 16
    assert bf["batch_checkpoint_flush_interval_seconds"] == 2.0
    assert bf["timeout"] == timedelta(minutes=30)


def test_backfill_column_tolerates_missing_column_on_drop():
    table = _Table(["video_id", "embedding"], drop_raises=True)
    _run(table)
    # drop raised and was swallowed; the rest still ran.
    assert "drop" not in table.calls
    assert "add" in table.calls
    assert "backfill" in table.calls


def test_backfill_column_incremental_keeps_existing_column():
    # reset=False on an existing column is non-destructive: no drop, no re-add.
    # The backfill fills only the null rows via the column's registered UDF — it
    # must NOT pass udf= (unsupported for remote/enterprise backfill).
    table = _Table(["video_id", "embedding"])
    _run(table, reset=False)
    assert "drop" not in table.calls
    assert "add" not in table.calls
    bf = table.calls["backfill"]
    assert bf["column"] == "embedding"
    assert "udf" not in bf
    # where is left to backfill's default ('<col> IS NULL'), so it's not forced here.
    assert "where" not in bf


def test_backfill_column_incremental_adds_column_when_missing():
    # First run (column absent): incremental still creates the column once (which
    # binds the UDF), then backfills — no drop, and no udf= override on backfill.
    table = _Table(["video_id"])
    _, sentinel_udf = _run(table, reset=False)
    assert "drop" not in table.calls
    assert table.calls["add"] == {"embedding": sentinel_udf}
    assert "udf" not in table.calls["backfill"]
