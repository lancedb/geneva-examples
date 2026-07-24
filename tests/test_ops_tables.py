"""CliRunner tests for the ``tables`` ops CLI (list / show / formats)."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime

from _fakes import FakeConn, FakeTable
from typer.testing import CliRunner

from geneva_examples.ops import tables as tables_cli

runner = CliRunner()

T0 = datetime(2026, 7, 23, 18, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 7, 23, 18, 5, 0, tzinfo=UTC)


def _patch_conn(monkeypatch, conn) -> None:
    monkeypatch.setattr(tables_cli, "_open_connection", lambda *a, **kw: (None, conn))


def _error_table(rows: int = 2) -> FakeTable:
    table = FakeTable(
        names=["error_id", "job_id", "error_type", "error_trace", "timestamp"],
        rows=rows,
    )
    table.list_data = [
        {
            "error_id": "e-old",
            "job_id": "j-1",
            "error_type": "ValueError",
            "error_trace": "Traceback:\n  boom old",
            "timestamp": T0,
        },
        {
            "error_id": "e-new",
            "job_id": "j-1",
            "error_type": "TimeoutError",
            "error_trace": "Traceback:\n  boom new",
            "timestamp": T1,
        },
    ]
    return table


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_includes_system_tables_and_counts(monkeypatch):
    conn = FakeConn(
        tables={
            "images": FakeTable(names=["image"], rows=5),
            "geneva_jobs": FakeTable(names=["job_id"], rows=2),
        },
        is_remote=False,
    )
    _patch_conn(monkeypatch, conn)
    result = runner.invoke(tables_cli.app, [])
    assert result.exit_code == 0, result.output
    assert "images" in result.output
    assert "5" in result.output
    assert "yes" in result.output  # geneva_jobs marked as a system table


def test_list_json_is_parseable(monkeypatch):
    conn = FakeConn(tables={"images": FakeTable(names=["image"], rows=5)})
    _patch_conn(monkeypatch, conn)
    result = runner.invoke(tables_cli.app, ["--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    entry = next(e for e in payload if e["table"] == "images")
    assert entry == {"table": "images", "system": False, "rows": 5}


def test_list_rejects_unknown_format(monkeypatch):
    _patch_conn(monkeypatch, FakeConn(tables={}))
    result = runner.invoke(tables_cli.app, ["--format", "yaml"])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_show_system_table_newest_first_job_id_leads(monkeypatch):
    conn = FakeConn(tables={"geneva_errors": _error_table()}, is_remote=False)
    _patch_conn(monkeypatch, conn)
    result = runner.invoke(tables_cli.app, ["show", "geneva_errors"])
    assert result.exit_code == 0, result.output
    assert "newest first" in result.output
    header = next(line for line in result.output.splitlines() if line.startswith("#"))
    assert header.split("|")[1].strip() == "job_id"  # promoted to first column
    body = [line for line in result.output.splitlines() if "| e-" in line]
    assert "e-new" in body[0] and "e-old" in body[1]  # newest first
    assert "boom new" not in result.output  # multiline cell truncated in ascii
    assert "Traceback: …" in result.output


def test_show_job_id_filter_builds_like_predicate(monkeypatch):
    table = _error_table()
    _patch_conn(monkeypatch, FakeConn(tables={"geneva_errors": table}))
    result = runner.invoke(
        tables_cli.app, ["show", "geneva_errors", "--job-id", " j-'1 "]
    )
    assert result.exit_code == 0, result.output
    assert table.wheres[0] == "(job_id LIKE '%j-1%')"


def test_show_where_and_job_id_combine(monkeypatch):
    table = _error_table()
    _patch_conn(monkeypatch, FakeConn(tables={"geneva_errors": table}))
    result = runner.invoke(
        tables_cli.app,
        ["show", "geneva_errors", "--job-id", "j-1", "--where", "attempt > 1"],
    )
    assert result.exit_code == 0, result.output
    assert table.wheres[0] == "(job_id LIKE '%j-1%') AND (attempt > 1)"


def test_show_job_id_rejected_on_plain_tables(monkeypatch):
    _patch_conn(monkeypatch, FakeConn(tables={"images": FakeTable(names=["a"])}))
    result = runner.invoke(tables_cli.app, ["show", "images", "--job-id", "x"])
    assert result.exit_code == 2
    assert "system tables" in result.output


def test_show_plain_table_where_passthrough(monkeypatch):
    table = FakeTable(names=["id", "score"], rows=9)
    table.list_data = [{"id": 7, "score": None}]
    _patch_conn(monkeypatch, FakeConn(tables={"debug_demo": table}))
    result = runner.invoke(
        tables_cli.app, ["show", "debug_demo", "--where", "score IS NULL"]
    )
    assert result.exit_code == 0, result.output
    assert table.wheres == ["(score IS NULL)"]
    assert "newest first" not in result.output


def test_show_select_projects_and_validates(monkeypatch):
    _patch_conn(monkeypatch, FakeConn(tables={"geneva_errors": _error_table()}))
    result = runner.invoke(
        tables_cli.app,
        ["show", "geneva_errors", "--select", "error_type", "--select", "job_id"],
    )
    assert result.exit_code == 0, result.output
    header = next(line for line in result.output.splitlines() if line.startswith("#"))
    assert "error_trace" not in header
    assert "error_type" in header

    bad = runner.invoke(tables_cli.app, ["show", "geneva_errors", "--select", "nope"])
    assert bad.exit_code == 2
    assert "unknown column" in bad.output


def test_show_csv_round_trips_multiline_values(monkeypatch):
    _patch_conn(monkeypatch, FakeConn(tables={"geneva_errors": _error_table()}))
    result = runner.invoke(tables_cli.app, ["show", "geneva_errors", "--format", "csv"])
    assert result.exit_code == 0, result.output
    rows = list(csv.reader(io.StringIO(result.stdout)))
    header, data = rows[0], rows[1:]
    trace = data[0][header.index("error_trace")]
    assert trace == "Traceback:\n  boom new"  # full value, newline preserved
    stamp = data[0][header.index("timestamp")]
    assert stamp == T1.isoformat()


def test_show_json_full_values(monkeypatch):
    _patch_conn(monkeypatch, FakeConn(tables={"geneva_errors": _error_table()}))
    result = runner.invoke(
        tables_cli.app, ["show", "geneva_errors", "--format", "json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload[0]["error_trace"] == "Traceback:\n  boom new"
    assert payload[0]["timestamp"] == T1.isoformat()


def test_show_cell_prints_full_value(monkeypatch):
    _patch_conn(monkeypatch, FakeConn(tables={"geneva_errors": _error_table()}))
    result = runner.invoke(
        tables_cli.app, ["show", "geneva_errors", "--cell", "0", "error_trace"]
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "Traceback:\n  boom new"

    bad_col = runner.invoke(
        tables_cli.app, ["show", "geneva_errors", "--cell", "0", "nope"]
    )
    assert bad_col.exit_code == 2
    bad_row = runner.invoke(
        tables_cli.app, ["show", "geneva_errors", "--cell", "99", "error_trace"]
    )
    assert bad_row.exit_code == 2


def test_show_unknown_table_exits_cleanly(monkeypatch):
    _patch_conn(monkeypatch, FakeConn(tables={}))
    result = runner.invoke(tables_cli.app, ["show", "nope"])
    assert result.exit_code == 1
    assert "cannot open table" in result.output


def test_show_defaults_to_local_mode(monkeypatch):
    seen = {}

    def spy(config, db_uri, log_level, mode=None):
        seen["mode"] = mode
        return None, FakeConn(tables={"t": FakeTable(names=["a"])})

    monkeypatch.setattr(tables_cli, "_open_connection", spy)
    result = runner.invoke(tables_cli.app, ["show", "t"])
    assert result.exit_code == 0, result.output
    assert seen["mode"] == "local"
