"""Tests for the bounded, re-vending HF streaming helpers.

The bounded iterator and the credential re-vend lever are exercised with
in-memory fakes -- no network, no `datasets`, no geneva connection.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from geneva_examples.core.utils import hf_streaming


def _batch(n: int, start: int = 0) -> pa.RecordBatch:
    """A RecordBatch of `n` rows with a monotonic `i` column starting at `start`."""
    return pa.RecordBatch.from_pylist([{"i": start + k} for k in range(n)])


def _rows(batches: list[pa.RecordBatch]) -> list[int]:
    """Flatten the `i` column across batches."""
    out: list[int] = []
    for b in batches:
        out.extend(b.column("i").to_pylist())
    return out


def _feed(monkeypatch, batches: list[pa.RecordBatch]) -> None:
    """Make `_raw_batches` yield the given in-memory batches."""
    monkeypatch.setattr(hf_streaming, "_raw_batches", lambda *a, **k: iter(batches))


def _iter(**kwargs) -> list[pa.RecordBatch]:
    kwargs.setdefault("mode", "datasets")
    return list(hf_streaming.iter_hf_batches("d", "train", 10, **kwargs))


# --- iter_hf_batches --------------------------------------------------------


def test_passes_batches_through_preserving_boundaries(monkeypatch):
    _feed(monkeypatch, [_batch(3, 0), _batch(2, 3)])
    out = _iter()
    assert _rows(out) == [0, 1, 2, 3, 4]
    assert [b.num_rows for b in out] == [3, 2]


def test_limit_none_yields_everything(monkeypatch):
    _feed(monkeypatch, [_batch(3, 0), _batch(3, 3)])
    assert _rows(_iter(limit=None)) == [0, 1, 2, 3, 4, 5]


def test_limit_truncates_exactly_mid_batch(monkeypatch):
    _feed(monkeypatch, [_batch(3, 0), _batch(3, 3)])
    assert _rows(_iter(limit=4)) == [0, 1, 2, 3]


def test_empty_input_yields_nothing(monkeypatch):
    _feed(monkeypatch, [])
    assert _iter() == []


def test_empty_batches_are_skipped(monkeypatch):
    _feed(monkeypatch, [_batch(0), _batch(2, 0)])
    assert _rows(_iter()) == [0, 1]


def test_skip_rows_drops_prefix_mid_batch(monkeypatch):
    _feed(monkeypatch, [_batch(3, 0), _batch(3, 3)])
    assert _rows(_iter(skip_rows=4)) == [4, 5]


def test_skip_rows_drops_whole_batches(monkeypatch):
    _feed(monkeypatch, [_batch(2, 0), _batch(2, 2), _batch(2, 4)])
    assert _rows(_iter(skip_rows=2)) == [2, 3, 4, 5]


def test_skip_and_limit_are_absolute(monkeypatch):
    # limit counts skipped rows too: resuming a --limit 7 load tops up to 7 total.
    _feed(monkeypatch, [_batch(10, 0)])
    assert _rows(_iter(skip_rows=3, limit=7)) == [3, 4, 5, 6]


def test_skip_equal_to_limit_yields_nothing(monkeypatch):
    _feed(monkeypatch, [_batch(10, 0)])
    assert _iter(skip_rows=5, limit=5) == []


def test_unknown_source_mode_raises(monkeypatch):
    with pytest.raises(ValueError, match="unknown source mode"):
        list(hf_streaming.iter_hf_batches("d", "train", 10, mode="bogus"))


# --- fresh_table re-vend lever ----------------------------------------------


class _FakeTable:
    def __init__(self, name: str = "t") -> None:
        self.name = name


class _FakeConn:
    def __init__(self) -> None:
        self.open_calls = 0

    def open_table(self, name: str) -> _FakeTable:
        self.open_calls += 1
        return _FakeTable(name)


class _FakeLtbl:
    def __init__(self, opts=None, raises: bool = False) -> None:
        self.opts = opts
        self.raises = raises
        self.calls = 0

    def latest_storage_options(self):
        self.calls += 1
        if self.raises:
            raise RuntimeError("provider unavailable")
        return self.opts


class _FakeTableWithLtbl:
    def __init__(self, ltbl: _FakeLtbl) -> None:
        self._ltbl = ltbl


def test_connect_mode_rebuilds_connection_each_call(monkeypatch):
    conns: list[_FakeConn] = []

    def fake_connect(cfg):
        conn = _FakeConn()
        conns.append(conn)
        return conn

    monkeypatch.setattr("geneva_examples.core.common.connect", fake_connect)

    c1, t1 = hf_streaming.fresh_table(object(), "tbl", mode="connect")
    c2, _t2 = hf_streaming.fresh_table(object(), "tbl", mode="connect")

    assert len(conns) == 2  # a fresh connection (=> fresh vend) per chunk
    assert c1 is conns[0] and c2 is conns[1]
    assert t1.name == "tbl" and c1.open_calls == 1


def test_reopen_mode_reuses_connection(monkeypatch):
    conn = _FakeConn()
    c, t = hf_streaming.fresh_table(object(), "tbl", mode="reopen", conn=conn)
    assert c is conn
    assert conn.open_calls == 1
    assert t.name == "tbl"


def test_reopen_mode_requires_conn():
    with pytest.raises(ValueError, match="reopen mode requires"):
        hf_streaming.fresh_table(object(), "tbl", mode="reopen")


def test_latest_mode_refreshes_in_place():
    ltbl = _FakeLtbl(opts={"aws_session_token": "x"})
    table = _FakeTableWithLtbl(ltbl)
    conn = _FakeConn()
    c, t = hf_streaming.fresh_table(
        object(), "tbl", mode="latest", conn=conn, table=table
    )
    assert c is conn and t is table
    assert ltbl.calls == 1
    assert conn.open_calls == 0  # reuses the held table, no re-open


def test_latest_mode_opens_when_no_table_and_survives_missing_primitive():
    # conn.open_table returns a plain table without _ltbl -> refresh is best-effort.
    conn = _FakeConn()
    c, t = hf_streaming.fresh_table(object(), "tbl", mode="latest", conn=conn)
    assert c is conn
    assert conn.open_calls == 1
    assert t.name == "tbl"


def test_latest_mode_requires_conn():
    with pytest.raises(ValueError, match="latest mode requires"):
        hf_streaming.fresh_table(object(), "tbl", mode="latest")


def test_unknown_revend_mode_raises():
    with pytest.raises(ValueError, match="unknown revend mode"):
        hf_streaming.fresh_table(object(), "tbl", mode="bogus")


def test_revend_loop_vends_once_per_chunk(monkeypatch):
    calls = {"n": 0}

    def fake_connect(cfg):
        calls["n"] += 1
        return _FakeConn()

    monkeypatch.setattr("geneva_examples.core.common.connect", fake_connect)

    conn = None
    table = None
    for _ in range(3):
        conn, table = hf_streaming.fresh_table(
            object(), "tbl", mode="connect", conn=conn, table=table
        )
    assert calls["n"] == 3


# --- vended_token_prefix ----------------------------------------------------


def test_token_prefix_returns_short_prefix():
    table = _FakeTableWithLtbl(_FakeLtbl(opts={"aws_session_token": "ABCDEFGHIJKL"}))
    assert hf_streaming.vended_token_prefix(table) == "ABCDEFGH"
    assert hf_streaming.vended_token_prefix(table, length=4) == "ABCD"


def test_token_prefix_none_when_token_missing():
    assert (
        hf_streaming.vended_token_prefix(_FakeTableWithLtbl(_FakeLtbl(opts={})))
        == "<none>"
    )
    assert (
        hf_streaming.vended_token_prefix(_FakeTableWithLtbl(_FakeLtbl(opts=None)))
        == "<none>"
    )


def test_token_prefix_none_on_error():
    assert hf_streaming.vended_token_prefix(object()) == "<none>"
    raising = _FakeTableWithLtbl(_FakeLtbl(raises=True))
    assert hf_streaming.vended_token_prefix(raising) == "<none>"
