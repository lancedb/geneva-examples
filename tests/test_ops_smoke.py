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

    cfg = types.SimpleNamespace(
        mode="enterprise",
        is_local=False,
        db_uri="db://test",
        local_db_path="./local_db",
        table_name="images",
    )
    monkeypatch.setattr(stats, "load_config", lambda _config, **_kw: cfg)
    monkeypatch.setattr(stats, "connect", lambda _cfg: conn)

    result = CliRunner().invoke(stats.app, [])

    assert result.exit_code == 0, result.output
    assert "location: db://test" in result.output
    assert "[videos]" in result.output
    assert "[video_clips]" in result.output


@pytest.mark.parametrize(
    ("module_name", "extra_argv"),
    [("stats", []), ("cleanup", ["--yes"]), ("jobs", [])],
)
def test_ops_cli_forwards_db_uri_override_to_load_config(
    module_name: str,
    extra_argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_geneva: None,
) -> None:
    """``--db-uri`` must reach ``load_config``, not be assigned onto the Config.

    Assigning after the load bypassed :func:`normalize_db_uri`, so a bare name
    silently became an on-disk database instead of a cluster connection.
    """
    import importlib

    module = importlib.import_module(f"geneva_examples.ops.{module_name}")
    captured: dict = {}

    def _fake_load_config(_config=None, **kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(
            mode="enterprise",
            is_local=False,
            db_uri="db://scratch",
            local_db_path="./local_db",
        )

    monkeypatch.setattr(module, "load_config", _fake_load_config)
    monkeypatch.setattr(module, "connect", lambda _cfg: FakeConn())

    result = CliRunner().invoke(module.app, [*extra_argv, "--db-uri", "scratch"])

    assert result.exit_code == 0, result.output
    assert captured.get("db_uri_override") == "scratch"


def test_stats_cli_reports_missing_table(
    monkeypatch: pytest.MonkeyPatch, fake_geneva: None
) -> None:
    # A connection that has no tables -> open_table raises -> "(table not found)".
    conn = FakeConn()

    cfg = types.SimpleNamespace(
        mode="enterprise",
        is_local=False,
        db_uri="db://test",
        local_db_path="./local_db",
        table_name="images",
    )
    monkeypatch.setattr(stats, "load_config", lambda _config, **_kw: cfg)
    monkeypatch.setattr(stats, "connect", lambda _cfg: conn)

    result = CliRunner().invoke(stats.app, [])

    assert result.exit_code == 0, result.output
    assert "(table not found)" in result.output
