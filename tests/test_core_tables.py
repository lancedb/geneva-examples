"""Tests for the shared table-viewer logic (core/tables.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from geneva_examples.core import tables as ct

T0 = datetime(2026, 7, 23, 18, 0, 0, tzinfo=UTC)


def _ts(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


class _Query:
    """Chainable query stub that pops one payload per to_list() call."""

    def __init__(self, table: _Table) -> None:
        self._table = table

    def where(self, expr: str) -> _Query:
        self._table.wheres.append(expr)
        return self

    def select(self, cols: list[str]) -> _Query:
        self._table.selects.append(list(cols))
        return self

    def limit(self, n: int) -> _Query:
        self._table.limits.append(n)
        return self

    def to_list(self) -> list[dict]:
        if self._table.payloads:
            return self._table.payloads.pop(0)
        return []


class _Table:
    """Programmable table: each to_list() returns the next payload."""

    def __init__(self, *payloads: list[dict]) -> None:
        self.payloads = list(payloads)
        self.wheres: list[str] = []
        self.selects: list[list[str]] = []
        self.limits: list[int] = []

    def search(self) -> _Query:
        return _Query(self)


class _Conn:
    def __init__(self, tables: dict | None = None, namespace=("__system",)) -> None:
        self._tables = tables or {}
        if namespace is not None:
            self.system_namespace = list(namespace)
        self.opens: list[tuple[str, dict]] = []

    def open_table(self, name: str, **kwargs):
        self.opens.append((name, kwargs))
        if name in self._tables:
            return self._tables[name]
        raise RuntimeError(f"table not found: {name}")


# ---------------------------------------------------------------------------
# fetch_newest_first
# ---------------------------------------------------------------------------


def _index_row(key: str, minutes: int | None) -> dict:
    return {"timestamp": None if minutes is None else _ts(minutes), "error_id": key}


def test_fetch_newest_first_orders_and_limits():
    index = [_index_row("a", 1), _index_row("b", 3), _index_row("c", 2)]
    fetched = [  # deliberately not newest-first: the fetch must restore order
        {"error_id": "c", "error_type": "Oc"},
        {"error_id": "b", "error_type": "Ob"},
    ]
    table = _Table(index, fetched)

    total, rows = ct.fetch_newest_first(
        table, ["error_id", "error_type"], None, "timestamp", "error_id", 2
    )

    assert total == 3
    assert [r["error_id"] for r in rows] == ["b", "c"]  # newest first
    assert table.selects[0] == ["timestamp", "error_id"]  # narrow index scan
    assert table.wheres == ["error_id IN ('b','c')"]
    assert table.limits == [2]


def test_fetch_newest_first_passes_filter_to_both_scans():
    table = _Table([_index_row("a", 1)], [{"error_id": "a"}])
    ct.fetch_newest_first(
        table, ["error_id"], "job_id LIKE '%x%'", "timestamp", "error_id", 5
    )
    assert table.wheres[0] == "job_id LIKE '%x%'"
    assert "error_id IN ('a')" in table.wheres[1]


def test_fetch_newest_first_empty_skips_second_scan():
    table = _Table([])
    assert ct.fetch_newest_first(
        table, ["error_id"], None, "timestamp", "error_id", 5
    ) == (0, [])
    assert len(table.selects) == 1  # no keyed fetch for an empty index


def test_fetch_newest_first_strips_quotes_from_keys():
    table = _Table([_index_row("a'b", 1)], [])
    ct.fetch_newest_first(table, ["error_id"], None, "timestamp", "error_id", 5)
    assert table.wheres == ["error_id IN ('ab')"]


def test_fetch_newest_first_null_timestamps_sort_last():
    index = [_index_row("none", None), _index_row("real", 1)]
    table = _Table(index, [{"error_id": "real"}, {"error_id": "none"}])
    _total, rows = ct.fetch_newest_first(
        table, ["error_id"], None, "timestamp", "error_id", 2
    )
    assert [r["error_id"] for r in rows] == ["real", "none"]


def test_fetch_newest_first_unknown_key_rows_go_last():
    table = _Table(
        [_index_row("a", 2), _index_row("b", 1)],
        [{"error_id": "stray"}, {"error_id": "a"}],
    )
    _total, rows = ct.fetch_newest_first(
        table, ["error_id"], None, "timestamp", "error_id", 2
    )
    assert [r["error_id"] for r in rows] == ["a", "stray"]


# ---------------------------------------------------------------------------
# open / probe / predicates / columns / detail
# ---------------------------------------------------------------------------


def test_open_any_table_plain_vs_system_namespace():
    table = _Table()
    conn = _Conn({"t": table, "geneva_jobs": table})
    assert ct.open_any_table(conn, "t") is table
    assert conn.opens[-1] == ("t", {})

    ct.open_any_table(conn, "geneva_jobs", system=True)
    assert conn.opens[-1] == ("geneva_jobs", {"namespace": ["__system"]})


def test_open_any_table_defaults_missing_namespace_to_empty():
    conn = _Conn({"geneva_jobs": _Table()}, namespace=None)
    ct.open_any_table(conn, "geneva_jobs", system=True)
    assert conn.opens[-1] == ("geneva_jobs", {"namespace": []})


def test_probe_system_tables_returns_only_present():
    conn = _Conn({"geneva_errors": _Table()})  # geneva_jobs raises
    assert ct.probe_system_tables(conn) == ["geneva_errors"]
    assert ct.probe_system_tables(_Conn({})) == []


def test_job_id_where():
    assert ct.job_id_where(None) is None
    assert ct.job_id_where("   ") is None
    assert ct.job_id_where(" c5'dd ") == "job_id LIKE '%c5dd%'"


def test_lead_with_job_id():
    cols = ["error_id", "job_id", "error_type"]
    assert ct.lead_with_job_id(cols) == ["job_id", "error_id", "error_type"]
    assert cols == ["error_id", "job_id", "error_type"]  # input not mutated
    assert ct.lead_with_job_id(["a", "b"]) == ["a", "b"]


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "(null)"),
        (b"abc", "<3 bytes>"),
        (bytearray(b"abcd"), "<4 bytes>"),
        ("plain\ntext", "plain\ntext"),
        (7, "7"),
    ],
)
def test_detail_text(value, expected):
    assert ct.detail_text(value) == expected
