"""Delete-table CLI: pick one table off the current backend and drop it.

Where ``cleanup`` drops the fixed set of example tables in one go, this is the
targeted version: it lists what's actually on the connection, asks which one to
delete (by name or by its number in the list), and confirms before dropping it.

Pass the name as an argument to skip the picker and ``--yes`` to skip the
confirmation, so the same command works from a script::

    uv run delete-table                 # list, pick, confirm
    uv run delete-table video_clips     # confirm only
    uv run delete-table video_clips -y  # no prompts

Chunker materialized views show up in the list as ``<name>_mv`` and are dropped
the same way — one at a time.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import typer

from geneva_examples.core.common import connect, setup_logging
from geneva_examples.core.config import load_config

logger = logging.getLogger(__name__)

app = typer.Typer(add_completion=False, help=__doc__)


def _row_count(conn: Any, name: str) -> int | None:
    """Row count for ``name``, or ``None`` if the table won't open/count.

    Only used to make the confirmation prompt informative, so any backend
    complaint here is worth swallowing — the drop itself is what matters.
    """
    try:
        return conn.open_table(name).count_rows()
    except Exception:  # noqa: BLE001 - purely cosmetic
        return None


def _pick_table(names: list[str]) -> str:
    """Print the numbered table list and prompt for one, by name or number."""
    typer.echo("Tables:")
    width = len(str(len(names)))
    for i, name in enumerate(names, start=1):
        typer.echo(f"  {i:>{width}}. {name}")

    choice = typer.prompt("Table to delete (name or number)").strip()
    # Name first, so a table literally called "2" still resolves to itself.
    if choice in names:
        return choice
    if choice.isdigit() and 1 <= int(choice) <= len(names):
        return names[int(choice) - 1]
    typer.secho(f"no such table: {choice!r}", fg="red", err=True)
    raise typer.Exit(code=1)


@app.command()
def run(
    table: str | None = typer.Argument(
        None, help="Table to delete. Omit to pick from the list."
    ),
    config: Path | None = typer.Option(None, "--config", help="Path to config.yaml."),
    mode: str | None = typer.Option(
        None, "--mode", help="Connection mode: 'local' or 'enterprise'."
    ),
    log_level: str = typer.Option("WARNING", help="Logging level (connection noise)."),
    db_uri: str | None = typer.Option(None, help="Override config db_uri."),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt."
    ),
) -> None:
    """Drop a single table, chosen from the tables on this backend."""
    setup_logging(log_level)

    import geneva  # noqa: F401  (ensures geneva is importable before connect)

    cfg = load_config(config, mode_override=mode, db_uri_override=db_uri)
    conn = connect(cfg)

    location = cfg.local_db_path if cfg.is_local else cfg.db_uri
    typer.echo(f"mode: {cfg.mode}   location: {location}")

    names = sorted(conn.table_names())
    if not names:
        typer.echo("  (no tables)")
        return

    if table is None:
        target = _pick_table(names)
    elif table in names:
        target = table
    else:
        typer.secho(f"no table named {table!r} on {location}", fg="red", err=True)
        raise typer.Exit(code=1)

    rows = _row_count(conn, target)
    typer.echo(f"  {target}: {rows} rows" if rows is not None else f"  {target}")

    if not yes:
        typer.confirm(f"Are you sure you want to delete {target}?", abort=True)

    conn.drop_table(target)
    typer.secho(f"dropped {target}", fg="yellow")
    logger.info("dropped %s", target)


if __name__ == "__main__":
    app()
