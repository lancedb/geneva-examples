"""Tables CLI: list tables and view rows — the TUI table viewer, pipeable.

Lists the database's tables (plus geneva's system tables) and dumps rows as
an ASCII table, CSV, or JSON. System tables (``geneva_jobs`` /
``geneva_errors``) read newest-first with an optional partial ``--job-id``
filter — the same behavior as the TUI viewer, backed by the same core logic.
Machine formats emit full, untruncated values and send the summary line to
stderr, so pipes stay clean::

    uv run tables show geneva_errors --job-id c5dd --format json | jq '.[].error_type'
    uv run tables show geneva_errors --cell 0 error_trace > trace.txt
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import typer

from geneva_examples.core.common import format_cell
from geneva_examples.core.tables import (
    DEFAULT_ROW_LIMIT,
    SYSTEM_TABLES,
    detail_text,
    fetch_newest_first,
    job_id_where,
    lead_with_job_id,
    open_any_table,
    probe_system_tables,
)
from geneva_examples.ops.jobs import _open_connection

logger = logging.getLogger(__name__)

app = typer.Typer(add_completion=False, help=__doc__)

_FORMATS = ("table", "csv", "json")

_CONFIG_OPT = typer.Option(None, "--config", help="Path to config.yaml.")
_MODE_OPT = typer.Option(
    "local",
    "--mode",
    help="Connection mode: 'local' (default) or 'enterprise'.",
)
_DB_URI_OPT = typer.Option(None, help="Override config db_uri.")
_LOG_LEVEL_OPT = typer.Option("WARNING", help="Logging level (connection noise).")
_FORMAT_OPT = typer.Option(
    "table", "--format", "-f", help="Output format: table (ascii), csv, or json."
)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _check_format(fmt: str) -> str:
    if fmt not in _FORMATS:
        typer.secho(
            f"unknown --format {fmt!r} (choose from {', '.join(_FORMATS)})",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=2)
    return fmt


def _machine_value(value: Any) -> Any:
    """Full value made JSON/CSV-safe: datetimes to ISO, bytes to a size tag."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    return value


def _render_ascii(cols: list[str], rows: list[dict]) -> str:
    """A plain ASCII table with a leading row-index column (for --cell)."""
    headers = ["#", *cols]
    body = [
        [str(i), *(format_cell(row.get(c)) for c in cols)] for i, row in enumerate(rows)
    ]
    widths = [
        max(len(header), *(len(line[j]) for line in body)) if body else len(header)
        for j, header in enumerate(headers)
    ]

    def _line(parts: list[str]) -> str:
        return " | ".join(
            p.ljust(w) for p, w in zip(parts, widths, strict=True)
        ).rstrip()

    rule = "-+-".join("-" * w for w in widths)
    return "\n".join([_line(headers), rule, *(_line(line) for line in body)])


def _render_csv(cols: list[str], rows: list[dict]) -> str:
    """CSV with full values; lists/dicts are JSON-encoded into their cell."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(cols)
    for row in rows:
        line = []
        for col in cols:
            value = _machine_value(row.get(col))
            if isinstance(value, (list, dict)):
                value = json.dumps(value, default=str)
            line.append("" if value is None else value)
        writer.writerow(line)
    return buffer.getvalue().rstrip("\n")


def _render_json(cols: list[str], rows: list[dict]) -> str:
    """A JSON array of row objects with full values."""
    payload = [{c: _machine_value(row.get(c)) for c in cols} for row in rows]
    return json.dumps(payload, indent=2, default=str)


def _emit(fmt: str, cols: list[str], rows: list[dict], summary: str) -> None:
    """Print rows in ``fmt``; machine formats keep stdout pure data."""
    if fmt == "table":
        typer.secho(summary, fg="bright_black")
        typer.echo(_render_ascii(cols, rows))
    else:
        typer.secho(summary, fg="bright_black", err=True)
        typer.echo(
            _render_csv(cols, rows) if fmt == "csv" else _render_json(cols, rows)
        )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.callback(invoke_without_command=True)
def list_tables(
    ctx: typer.Context,
    config: Path | None = _CONFIG_OPT,
    mode: str | None = _MODE_OPT,
    db_uri: str | None = _DB_URI_OPT,
    log_level: str = _LOG_LEVEL_OPT,
    fmt: str = _FORMAT_OPT,
) -> None:
    """List tables (including geneva's system tables) with row counts."""
    if ctx.invoked_subcommand is not None:
        return
    fmt = _check_format(fmt)
    _cfg, conn = _open_connection(config, db_uri, log_level, mode)

    def _count(name: str, system: bool) -> int | None:
        try:
            return open_any_table(conn, name, system=system).count_rows()
        except Exception as exc:  # noqa: BLE001 — a broken table shouldn't hide the rest
            logger.warning("count_rows(%s) failed: %s", name, exc)
            return None

    entries = [
        {"table": name, "system": False, "rows": _count(name, False)}
        for name in sorted(conn.table_names())
    ]
    entries += [
        {"table": name, "system": True, "rows": _count(name, True)}
        for name in probe_system_tables(conn)
    ]
    cols = ["table", "system", "rows"]
    summary = f"{len(entries)} table(s)"
    if fmt == "table":
        display = [
            {**e, "system": "yes" if e["system"] else "", "rows": e["rows"]}
            for e in entries
        ]
        _emit(fmt, cols, display, summary)
    else:
        _emit(fmt, cols, entries, summary)


@app.command()
def show(
    name: str = typer.Argument(
        ..., help="Table name; geneva_jobs / geneva_errors are system tables."
    ),
    config: Path | None = _CONFIG_OPT,
    mode: str | None = _MODE_OPT,
    db_uri: str | None = _DB_URI_OPT,
    log_level: str = _LOG_LEVEL_OPT,
    fmt: str = _FORMAT_OPT,
    job_id: str | None = typer.Option(
        None, "--job-id", help="Partial job_id filter (system tables only)."
    ),
    where: str | None = typer.Option(
        None, "--where", help="SQL predicate applied to the scan."
    ),
    limit: int = typer.Option(DEFAULT_ROW_LIMIT, help="Max rows to fetch."),
    select: Optional[list[str]] = typer.Option(  # noqa: UP045 — typer needs Optional
        None, "--select", help="Column to include (repeatable; default: all)."
    ),
    cell: Optional[tuple[int, str]] = typer.Option(  # noqa: UP045
        None,
        "--cell",
        metavar="ROW COLUMN",
        help="Print one full untruncated value (by displayed row index) and exit.",
    ),
) -> None:
    """Show a table's rows (system tables read newest-first)."""
    fmt = _check_format(fmt)
    is_system = name in SYSTEM_TABLES
    if job_id and not is_system:
        typer.secho(
            f"--job-id only applies to the system tables ({', '.join(SYSTEM_TABLES)})",
            fg="red",
            err=True,
        )
        raise typer.Exit(code=2)

    _cfg, conn = _open_connection(config, db_uri, log_level, mode)
    try:
        table = open_any_table(conn, name, system=is_system)
        schema_cols = list(table.schema.names)
    except Exception as exc:  # noqa: BLE001 — one clean message, not a stack
        typer.secho(f"cannot open table {name!r}: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from None

    cols = lead_with_job_id(schema_cols) if is_system else schema_cols
    if select:
        missing = [c for c in select if c not in schema_cols]
        if missing:
            typer.secho(f"unknown column(s): {', '.join(missing)}", fg="red", err=True)
            raise typer.Exit(code=2)
        cols = list(select)

    predicates = [p for p in (job_id_where(job_id), where) if p]
    combined = " AND ".join(f"({p})" for p in predicates) if predicates else None

    try:
        if is_system:
            ts_col, key_col = SYSTEM_TABLES[name]
            # The key column must ride along for order restoration even when
            # a --select projection drops it from the output.
            fetch_cols = cols if key_col in cols else [*cols, key_col]
            total, rows = fetch_newest_first(
                table, fetch_cols, combined, ts_col, key_col, limit
            )
        else:
            total = table.count_rows(combined) if combined else table.count_rows()
            query = table.search()
            if combined:
                query = query.where(combined)
            rows = query.select(cols).limit(limit).to_list()
    except Exception as exc:  # noqa: BLE001 — bad --where etc.: report and exit
        typer.secho(f"query failed: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from None

    if cell is not None:
        row_index, column = cell
        if column not in cols:
            typer.secho(f"unknown column {column!r}", fg="red", err=True)
            raise typer.Exit(code=2)
        if not 0 <= row_index < len(rows):
            typer.secho(
                f"row {row_index} out of range (showing {len(rows)})",
                fg="red",
                err=True,
            )
            raise typer.Exit(code=2)
        typer.echo(detail_text(rows[row_index].get(column)))
        return

    order = " · newest first" if is_system else ""
    summary = f"{name} — {total} rows × {len(cols)} cols (showing {len(rows)}{order})"
    if combined:
        summary += f" where {combined}"
    _emit(fmt, cols, rows, summary)
