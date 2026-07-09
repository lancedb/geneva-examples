"""Cleanup CLI: drop the video + clips tables (and their MV siblings).

Drops ``videos`` and ``video_clips`` plus their transient ``<name>_mv``
materialized views (and, with ``--pdfs-table``, the ``pdfs`` table), so you can
start a fresh ingest/chunk run. Prompts for confirmation unless ``--yes`` is
passed.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import typer

from geneva_examples.core.common import connect, setup_logging
from geneva_examples.core.config import load_config

logger = logging.getLogger(__name__)

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def run(
    config: Path | None = typer.Option(None, "--config", help="Path to config.yaml."),
    mode: str | None = typer.Option(
        None, "--mode", help="Connection mode: 'local' or 'enterprise'."
    ),
    log_level: str = typer.Option("INFO", help="Logging level."),
    db_uri: str | None = typer.Option(None, help="Override config db_uri."),
    videos_table: str = typer.Option("videos", help="Videos table to drop."),
    clips_table: str = typer.Option("video_clips", help="Clips table to drop."),
    pdfs_table: str | None = typer.Option(
        None, help="Also drop this PDFs table (e.g. 'pdfs')."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt."
    ),
) -> None:
    """Drop the video/clip tables (and their MV siblings)."""
    setup_logging(log_level)
    os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")

    cfg = load_config(config, mode_override=mode)
    if db_uri:
        cfg.db_uri = db_uri
    location = cfg.local_db_path if cfg.is_local else cfg.db_uri

    # Preserve order while de-duplicating (e.g. if videos_table == clips_table).
    candidates = [
        videos_table,
        f"{videos_table}_mv",
        clips_table,
        f"{clips_table}_mv",
    ]
    if pdfs_table:
        candidates.append(pdfs_table)
    targets: list[str] = []
    for name in candidates:
        if name not in targets:
            targets.append(name)

    typer.echo(f"About to drop the following tables from {location}:")
    for name in targets:
        typer.echo(f"  - {name}")
    if not yes:
        typer.confirm("Proceed? This permanently deletes the tables.", abort=True)

    conn = connect(cfg)
    for name in targets:
        try:
            conn.drop_table(name)
            logger.info("dropped %s", name)
        except Exception:  # noqa: BLE001
            logger.info("skip_missing %s", name)

    logger.info("cleanup_ok")


if __name__ == "__main__":
    app()
