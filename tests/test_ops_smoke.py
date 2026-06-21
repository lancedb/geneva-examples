"""Smoke test for the `stats` ops CLI wiring.

The ops CLIs are excluded from the coverage gate (live connection), and the unit
tests cover only their formatting helpers. This drives `stats` end-to-end through
``CliRunner`` with the cluster boundary mocked, confirming it connects and
summarizes both tables without error.
"""

from __future__ import annotations

import types

import pytest
from _fakes import FakeConn, FakeTable
from typer.testing import CliRunner

from geneva_examples.ops import stats


def test_stats_cli_summarizes_tables(
    monkeypatch: pytest.MonkeyPatch, fake_geneva: None
) -> None:
    videos = FakeTable(names=["video_id", "uri"], rows=2)
    clips = FakeTable(
        names=["video_id", "chunk_id", "start_sec", "end_sec", "embedding"], rows=5
    )
    conn = FakeConn(tables={"videos": videos, "video_clips": clips})

    cfg = types.SimpleNamespace(db_uri="db://test", table_name="images")
    monkeypatch.setattr(stats, "load_config", lambda _config: cfg)
    monkeypatch.setattr(stats, "connect", lambda _cfg: conn)

    result = CliRunner().invoke(stats.app, [])

    assert result.exit_code == 0, result.output
    assert "db_uri: db://test" in result.output
    assert "[videos]" in result.output
    assert "[video_clips]" in result.output


def test_stats_cli_reports_missing_table(
    monkeypatch: pytest.MonkeyPatch, fake_geneva: None
) -> None:
    # A connection that has no tables -> open_table raises -> "(table not found)".
    conn = FakeConn()

    cfg = types.SimpleNamespace(db_uri="db://test", table_name="images")
    monkeypatch.setattr(stats, "load_config", lambda _config: cfg)
    monkeypatch.setattr(stats, "connect", lambda _cfg: conn)

    result = CliRunner().invoke(stats.app, [])

    assert result.exit_code == 0, result.output
    assert "(table not found)" in result.output
