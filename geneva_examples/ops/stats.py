"""Stats CLI: summarize LanceDB tables (row counts, schema, feature columns)."""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path
from typing import Any

import typer

from geneva_examples.core.common import connect, setup_logging
from geneva_examples.core.config import load_config

logger = logging.getLogger(__name__)

app = typer.Typer(add_completion=False, help=__doc__)

# Feature columns the stages may add, across the image and video workflows.
_FEATURE_COLUMNS = (
    "file_size",  # images: lightweight
    "dimensions",  # images: lightweight
    "embedding",  # images / clips: (frame-)embed
    "caption",  # clips: frame-caption
    "pose",  # clips: frame-openpose
    "caption_blip",  # images: caption
    "caption_blip_v2",  # images: caption
)
# Id-like columns, most specific first, used for the per-table id sample.
_ID_COLUMNS = ("video_id", "image_id", "doc_id")
# Tables summarized by default (each is skipped cleanly if absent).
_DEFAULT_TABLES = ("images", "videos", "video_clips")


def _open(conn: Any, name: str):
    """Open a table by name, or return None if it doesn't exist."""
    try:
        return conn.open_table(name)
    except Exception:  # noqa: BLE001
        return None


def _schema_lines(table: Any) -> list[str]:
    return [f"    {f.name}: {f.type}" for f in table.schema]


def _report_features(table: Any, names: set[str], total: int) -> None:
    """Print each feature column present + how many rows are populated (non-null)."""
    present = [c for c in _FEATURE_COLUMNS if c in names]
    if not present:
        typer.echo("  feature columns: none yet")
        return
    typer.echo("  feature columns:")
    for col in present:
        try:
            nulls = table.count_rows(f"`{col}` IS NULL")
            typer.echo(f"    {col}: {total - nulls}/{total} populated")
        except Exception as exc:  # pragma: no cover - backend quirk  # noqa: BLE001
            typer.echo(f"    {col}: (null count unavailable: {type(exc).__name__})")


def _report_id_sample(table: Any, id_col: str, total: int) -> None:
    """Show a few values of a table's primary id column."""
    ids = [r[id_col] for r in table.search(None).select([id_col]).limit(5).to_list()]
    more = f" (+{total - len(ids)} more)" if total > len(ids) else ""
    typer.echo(
        f"  {id_col}s (showing {len(ids)} of {total}): {', '.join(map(str, ids))}{more}"
    )


def _report_clip_stats(table: Any, names: set[str], max_rows: int) -> None:
    """Per-video clip counts + chunk-duration stats (video_clips only).

    Caps the client-side scan at ``max_rows`` so ``stats`` on a large table can't
    pull every row to the driver; notes when the numbers are from a sample.
    """
    cols = [c for c in ("video_id", "start_sec", "end_sec") if c in names]
    total = table.count_rows()
    rows = table.search(None).select(cols).limit(max_rows).to_list()
    if len(rows) < total:
        typer.echo(
            f"  (per-video stats sampled from the first {len(rows)} of {total} rows)"
        )
    per_video = Counter(r["video_id"] for r in rows)
    shown = sorted(per_video.items())[:5]
    more = (
        f" (+{len(per_video) - len(shown)} more)" if len(per_video) > len(shown) else ""
    )
    typer.echo(f"  clips per video (showing {len(shown)} of {len(per_video)}):{more}")
    for vid, n in shown:
        typer.echo(f"    {vid}: {n}")
    durations = [
        float(r["end_sec"]) - float(r["start_sec"])
        for r in rows
        if r.get("end_sec") is not None and r.get("start_sec") is not None
    ]
    if durations:
        typer.echo(
            "  chunk seconds: "
            f"count={len(durations)} total={sum(durations):.1f} "
            f"min={min(durations):.1f} max={max(durations):.1f} "
            f"avg={sum(durations) / len(durations):.2f}"
        )


def _report_caption_sample(table: Any, names: set[str], sample: int) -> None:
    """Preview a few caption values, keyed by whatever id columns exist."""
    caption_col = next((c for c in ("caption", "caption_blip") if c in names), None)
    if not caption_col or sample <= 0:
        return
    id_cols = [c for c in ("video_id", "chunk_id", "image_id") if c in names]
    preview = table.search().select([*id_cols, caption_col]).limit(sample).to_list()
    typer.echo(f"  {caption_col} sample:")
    for r in preview:
        ident = " ".join(str(r.get(c)) for c in id_cols) or "?"
        typer.echo(f"    {ident}: {r.get(caption_col)!r}")


def _summarize_table(table: Any, sample: int, max_rows: int = 100_000) -> None:
    """Summarize any table: rows, schema, feature columns, and per-modality stats."""
    names = set(table.schema.names)
    total = table.count_rows()
    typer.echo(f"  rows: {total}")
    typer.echo("  schema:")
    for line in _schema_lines(table):
        typer.echo(line)

    _report_features(table, names, total)

    if {"video_id", "start_sec", "end_sec"}.issubset(names):
        _report_clip_stats(table, names, max_rows)
    else:
        id_col = next((c for c in _ID_COLUMNS if c in names), None)
        if id_col:
            _report_id_sample(table, id_col, total)

    _report_caption_sample(table, names, sample)


@app.command()
def run(
    config: Path | None = typer.Option(None, "--config", help="Path to config.yaml."),
    log_level: str = typer.Option("WARNING", help="Logging level (connection noise)."),
    db_uri: str | None = typer.Option(None, help="Override config db_uri."),
    table: list[str] = typer.Option(
        None,
        "--table",
        help="Table to summarize (repeatable). Default: images, videos, video_clips.",
    ),
    sample: int = typer.Option(5, help="Caption rows to sample (0 to skip)."),
    max_rows: int = typer.Option(
        100_000, help="Cap rows scanned client-side for per-video stats."
    ),
) -> None:
    """Print stats for one or more tables (defaults to the example tables)."""
    setup_logging(log_level)

    import geneva  # noqa: F401  (ensures geneva is importable before connect)

    cfg = load_config(config)
    if db_uri:
        cfg.db_uri = db_uri

    conn = connect(cfg)

    typer.echo(f"db_uri: {cfg.db_uri}")

    for name in table or list(_DEFAULT_TABLES):
        typer.echo(f"\n[{name}]")
        opened = _open(conn, name)
        if opened is None:
            typer.echo("  (table not found)")
        else:
            _summarize_table(opened, sample, max_rows)


if __name__ == "__main__":
    app()
