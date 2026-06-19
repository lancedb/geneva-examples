"""Tests for core helpers: memory sizing, retry, schema-wait."""

from __future__ import annotations

import pytest

from geneva_examples.core import common
from geneva_examples.core.utils import retry, tables


def test_setup_logging_runs():
    common.setup_logging("DEBUG")  # configures the root logger without raising


def test_memory_request_bytes_normal():
    assert common.memory_request_bytes(1) == 1024**3


def test_memory_request_bytes_caps_to_32bit(caplog):
    capped = common.memory_request_bytes(4)  # 4 GiB > 2**31-1
    assert capped == 2**31 - 1


def test_retry_io_returns_on_first_success():
    assert retry.retry_io("op", lambda: 42) == 42


def test_retry_io_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(retry.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")
        return "ok"

    assert retry.retry_io("op", flaky, attempts=5, sleep_s=0.01) == "ok"
    assert calls["n"] == 3


def test_retry_io_raises_after_exhausting(monkeypatch):
    monkeypatch.setattr(retry.time, "sleep", lambda _s: None)

    def always_fails():
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        retry.retry_io("op", always_fails, attempts=3, sleep_s=0.01)


class _Schema:
    def __init__(self, names):
        self.names = names


class _Table:
    def __init__(self, names):
        self.schema = _Schema(names)


class _Conn:
    """Returns a table whose columns appear only on the Nth open_table call."""

    def __init__(self, names_per_call):
        self._names_per_call = list(names_per_call)
        self.calls = 0

    def open_table(self, _name):
        names = self._names_per_call[min(self.calls, len(self._names_per_call) - 1)]
        self.calls += 1
        return _Table(names)


def test_wait_for_columns_returns_when_present(monkeypatch):
    monkeypatch.setattr(tables.time, "sleep", lambda _s: None)
    conn = _Conn([[], ["a", "b"]])  # missing first, present second
    opened = tables.wait_for_columns(
        conn=conn, table_name="t", required={"a"}, attempts=5, sleep_s=0
    )
    assert "a" in opened.schema.names
    assert conn.calls == 2


def test_wait_for_columns_times_out(monkeypatch):
    monkeypatch.setattr(tables.time, "sleep", lambda _s: None)
    conn = _Conn([[]])  # never has the column
    with pytest.raises(RuntimeError, match="required columns not visible"):
        tables.wait_for_columns(
            conn=conn, table_name="t", required={"z"}, attempts=3, sleep_s=0
        )
