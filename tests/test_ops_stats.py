"""Tests for ops/stats table summarizers (with fake LanceDB tables)."""

from __future__ import annotations

import re

from geneva_examples.ops import stats


class _Field:
    def __init__(self, name, type_):
        self.name, self.type = name, type_


class _Schema:
    def __init__(self, fields: dict):
        self._fields = [_Field(n, t) for n, t in fields.items()]
        self.names = list(fields)

    def __iter__(self):
        return iter(self._fields)


class _Query:
    def __init__(self, rows):
        self._rows = rows
        self._cols = None

    def select(self, cols):
        self._cols = cols
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def to_list(self):
        if self._cols is None:
            return list(self._rows)
        return [{c: r.get(c) for c in self._cols} for r in self._rows]


class _Table:
    def __init__(self, fields: dict, rows: list[dict]):
        self.schema = _Schema(fields)
        self._rows = rows

    def count_rows(self, where: str | None = None) -> int:
        if not where:
            return len(self._rows)
        col = re.search(r"`([^`]+)`", where).group(1)
        return sum(1 for r in self._rows if r.get(col) is None)

    def search(self, _expr=None):
        return _Query(list(self._rows))


class _Conn:
    def __init__(self, tables):
        self._tables = tables

    def open_table(self, name):
        if name not in self._tables:
            raise ValueError(f"no such table {name}")
        return self._tables[name]


def test_open_returns_none_on_error():
    conn = _Conn({"videos": _Table({"video_id": "string"}, [])})
    assert stats._open(conn, "videos") is not None
    assert stats._open(conn, "missing") is None


def test_schema_lines():
    table = _Table({"video_id": "string", "n": "int64"}, [])
    assert stats._schema_lines(table) == [
        "    video_id: string",
        "    n: int64",
    ]


def test_summarize_videos(capsys):
    table = _Table(
        {"video_id": "string", "video": "binary"},
        [{"video_id": f"v{i}"} for i in range(7)],
    )
    stats._summarize_videos(table)
    out = capsys.readouterr().out
    assert "rows: 7" in out
    assert "video_ids (showing 5 of 7)" in out
    assert "(+2 more)" in out


def test_summarize_clips_reports_features_and_durations(capsys):
    rows = [
        {
            "video_id": "a",
            "chunk_id": 0,
            "start_sec": 0.0,
            "end_sec": 1.0,
            "embedding": [0.1],
        },
        {
            "video_id": "a",
            "chunk_id": 1,
            "start_sec": 1.0,
            "end_sec": 2.0,
            "embedding": None,
        },
        {
            "video_id": "b",
            "chunk_id": 0,
            "start_sec": 0.0,
            "end_sec": 2.0,
            "embedding": [0.2],
        },
    ]
    fields = {
        "video_id": "string",
        "chunk_id": "int32",
        "start_sec": "float",
        "end_sec": "float",
        "embedding": "list<float>",
    }
    stats._summarize_clips(_Table(fields, rows), sample=0)
    out = capsys.readouterr().out
    assert "rows: 3" in out
    assert "feature columns:" in out
    assert "embedding: 2/3 populated" in out  # one null
    assert "clips per video" in out
    assert "chunk seconds:" in out


def test_summarize_clips_no_features(capsys):
    stats._summarize_clips(_Table({"video_id": "string"}, []), sample=0)
    assert "feature columns: none yet" in capsys.readouterr().out
