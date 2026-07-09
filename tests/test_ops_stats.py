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
    stats._summarize_table(table, sample=0)
    out = capsys.readouterr().out
    assert "rows: 7" in out
    assert "video_ids (showing 5 of 7)" in out
    assert "(+2 more)" in out


def test_summarize_images_reports_features_and_captions(capsys):
    rows = [
        {"image_id": "a", "file_size": 100, "caption_blip": "a cat"},
        {"image_id": "b", "file_size": None, "caption_blip": "a dog"},
    ]
    fields = {
        "image_id": "string",
        "file_size": "int64",
        "caption_blip": "string",
    }
    stats._summarize_table(_Table(fields, rows), sample=5)
    out = capsys.readouterr().out
    assert "rows: 2" in out
    assert "image_ids (showing 2 of 2)" in out
    assert "file_size: 1/2 populated" in out  # one null
    assert "caption_blip sample:" in out
    assert "a cat" in out


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
    stats._summarize_table(_Table(fields, rows), sample=0)
    out = capsys.readouterr().out
    assert "rows: 3" in out
    assert "feature columns:" in out
    assert "embedding: 2/3 populated" in out  # one null
    assert "clips per video" in out
    assert "chunk seconds:" in out


def test_summarize_clips_no_features(capsys):
    stats._summarize_table(_Table({"video_id": "string"}, []), sample=0)
    assert "feature columns: none yet" in capsys.readouterr().out


def test_clip_stats_notes_sampling_when_capped(capsys):
    rows = [
        {"video_id": f"v{i % 2}", "start_sec": 0.0, "end_sec": 1.0} for i in range(10)
    ]
    fields = {"video_id": "string", "start_sec": "float", "end_sec": "float"}
    # max_rows=4 < 10 total -> the per-video numbers come from a sample.
    stats._summarize_table(_Table(fields, rows), sample=0, max_rows=4)
    out = capsys.readouterr().out
    assert "sampled from the first 4 of 10 rows" in out
