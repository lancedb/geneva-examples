"""Smoke tests for the `delete-table` ops CLI.

The ops CLIs are excluded from the coverage gate (live connection), so these
drive the picker end-to-end through ``CliRunner`` with the cluster boundary
mocked: listing, selecting by number or name, the confirmation prompt, and the
refusals. What matters is that nothing gets dropped unless the user said so.
"""

from __future__ import annotations

import types

import pytest
from _fakes import FakeConn, FakeTable
from typer.testing import CliRunner

from geneva_examples.ops import delete_table


def _conn() -> FakeConn:
    return FakeConn(
        tables={
            "videos": FakeTable(names=["video_id"], rows=2),
            "video_clips": FakeTable(names=["video_id", "chunk_id"], rows=5),
            "images": FakeTable(names=["image_id"], rows=3),
        }
    )


@pytest.fixture
def conn(monkeypatch: pytest.MonkeyPatch) -> FakeConn:
    """A fake backend with three tables, wired into the CLI's config+connect."""
    fake = _conn()
    cfg = types.SimpleNamespace(
        mode="enterprise",
        is_local=False,
        db_uri="db://test",
        local_db_path="./local_db",
    )
    monkeypatch.setattr(delete_table, "load_config", lambda _config, **_kw: cfg)
    monkeypatch.setattr(delete_table, "connect", lambda _cfg: fake)
    return fake


def test_lists_tables_and_deletes_the_selected_number(
    conn: FakeConn, fake_geneva: None
) -> None:
    # Sorted listing: 1. images  2. video_clips  3. videos -> pick #3, confirm.
    result = CliRunner().invoke(delete_table.app, [], input="3\ny\n")

    assert result.exit_code == 0, result.output
    assert "1. images" in result.output
    assert "3. videos" in result.output
    assert "Are you sure you want to delete videos?" in result.output
    assert conn.dropped == ["videos"]


def test_selecting_by_name_deletes_that_table(
    conn: FakeConn, fake_geneva: None
) -> None:
    result = CliRunner().invoke(delete_table.app, [], input="video_clips\ny\n")

    assert result.exit_code == 0, result.output
    assert conn.dropped == ["video_clips"]


def test_table_argument_skips_the_picker_but_still_confirms(
    conn: FakeConn, fake_geneva: None
) -> None:
    result = CliRunner().invoke(delete_table.app, ["images"], input="y\n")

    assert result.exit_code == 0, result.output
    assert "Tables:" not in result.output
    assert "Are you sure you want to delete images?" in result.output
    assert conn.dropped == ["images"]


def test_yes_flag_skips_the_confirmation(conn: FakeConn, fake_geneva: None) -> None:
    result = CliRunner().invoke(delete_table.app, ["images", "--yes"])

    assert result.exit_code == 0, result.output
    assert "Are you sure" not in result.output
    assert conn.dropped == ["images"]


def test_declining_the_confirmation_drops_nothing(
    conn: FakeConn, fake_geneva: None
) -> None:
    result = CliRunner().invoke(delete_table.app, [], input="images\nn\n")

    assert result.exit_code != 0
    assert conn.dropped == []


def test_unknown_name_at_the_prompt_drops_nothing(
    conn: FakeConn, fake_geneva: None
) -> None:
    result = CliRunner().invoke(delete_table.app, [], input="nope\n")

    assert result.exit_code == 1
    assert "no such table" in result.output
    assert conn.dropped == []


def test_out_of_range_number_drops_nothing(conn: FakeConn, fake_geneva: None) -> None:
    result = CliRunner().invoke(delete_table.app, [], input="9\n")

    assert result.exit_code == 1
    assert conn.dropped == []


def test_unknown_table_argument_drops_nothing(
    conn: FakeConn, fake_geneva: None
) -> None:
    result = CliRunner().invoke(delete_table.app, ["nope", "--yes"])

    assert result.exit_code == 1
    assert "no table named 'nope'" in result.output
    assert conn.dropped == []


def test_empty_backend_reports_no_tables(
    monkeypatch: pytest.MonkeyPatch, fake_geneva: None
) -> None:
    empty = FakeConn()
    cfg = types.SimpleNamespace(
        mode="local",
        is_local=True,
        db_uri="db://test",
        local_db_path="./local_db",
    )
    monkeypatch.setattr(delete_table, "load_config", lambda _config, **_kw: cfg)
    monkeypatch.setattr(delete_table, "connect", lambda _cfg: empty)

    result = CliRunner().invoke(delete_table.app, [])

    assert result.exit_code == 0, result.output
    assert "(no tables)" in result.output
    assert empty.dropped == []
