"""Stats CLI: summarize the videos and video_clips tables."""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path

import typer

from geneva_examples.core.common import connect, setup_logging
from geneva_examples.core.config import load_config

logger = logging.getLogger(__name__)

app = typer.Typer(add_completion=False, help=__doc__)

# Feature columns the frame stages may add to video_clips.
_FEATURE_COLUMNS = ("embedding", "caption", "pose", "caption_blip", "caption_blip_v2")


def _open(conn: object, name: str):
    """Open a table by name, or return None if it doesn't exist."""
    try:
        return conn.open_table(name)
    except Exception:  # noqa: BLE001
        return None


def _schema_lines(table: object) -> list[str]:
    return [f"    {f.name}: {f.type}" for f in table.schema]


def _summarize_videos(table: object) -> None:
    typer.echo(f"  rows: {table.count_rows()}")
    typer.echo("  schema:")
    for line in _schema_lines(table):
        typer.echo(line)
    if "video_id" in table.schema.names:
        total = table.count_rows()
        ids = [
            r["video_id"]
            for r in table.search(None).select(["video_id"]).limit(5).to_list()
        ]
        more = f" (+{total - len(ids)} more)" if total > len(ids) else ""
        typer.echo(
            f"  video_ids (showing {len(ids)} of {total}): "
            f"{', '.join(map(str, ids))}{more}"
        )


def _summarize_clips(table: object, sample: int) -> None:
    names = set(table.schema.names)
    total = table.count_rows()
    typer.echo(f"  rows: {total}")
    typer.echo("  schema:")
    for line in _schema_lines(table):
        typer.echo(line)

    # Feature columns present + their null counts.
    present = [c for c in _FEATURE_COLUMNS if c in names]
    if present:
        typer.echo("  feature columns:")
        for col in present:
            try:
                nulls = table.count_rows(f"`{col}` IS NULL")
                typer.echo(f"    {col}: {total - nulls}/{total} populated")
            except Exception as exc:  # pragma: no cover - backend quirk  # noqa: BLE001
                typer.echo(f"    {col}: (null count unavailable: {type(exc).__name__})")
    else:
        typer.echo("  feature columns: none yet (run the frame-* stages)")

    # Per-video clip counts + chunk-duration stats (small columns only).
    cols = [c for c in ("video_id", "start_sec", "end_sec") if c in names]
    if {"video_id", "start_sec", "end_sec"}.issubset(names):
        rows = table.search(None).select(cols).to_list()
        per_video = Counter(r["video_id"] for r in rows)
        shown = sorted(per_video.items())[:5]
        more = (
            f" (+{len(per_video) - len(shown)} more)"
            if len(per_video) > len(shown)
            else ""
        )
        typer.echo(
            f"  clips per video (showing {len(shown)} of {len(per_video)}):{more}"
        )
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

    # Optional caption sample.
    caption_col = next((c for c in ("caption", "caption_blip") if c in names), None)
    if caption_col and sample > 0:
        preview = (
            table.search()
            .select(["video_id", "chunk_id", caption_col])
            .limit(sample)
            .to_list()
        )
        typer.echo(f"  {caption_col} sample:")
        for r in preview:
            typer.echo(
                f"    {r.get('video_id')} #{r.get('chunk_id')}: {r.get(caption_col)!r}"
            )


@app.command()
def run(
    config: Path | None = typer.Option(None, "--config", help="Path to config.yaml."),
    log_level: str = typer.Option("WARNING", help="Logging level (connection noise)."),
    db_uri: str | None = typer.Option(None, help="Override config db_uri."),
    videos_table: str = typer.Option("videos", help="Videos table name."),
    clips_table: str = typer.Option("video_clips", help="Clips table name."),
    sample: int = typer.Option(5, help="Caption rows to sample (0 to skip)."),
) -> None:
    """Print stats for the videos and video_clips tables."""
    setup_logging(log_level)

    import geneva  # noqa: F401  (ensures geneva is importable before connect)

    cfg = load_config(config)
    if db_uri:
        cfg.db_uri = db_uri

    conn = connect(cfg)

    typer.echo(f"db_uri: {cfg.db_uri}")

    typer.echo(f"\n[{videos_table}]")
    videos = _open(conn, videos_table)
    if videos is None:
        typer.echo("  (table not found)")
    else:
        _summarize_videos(videos)

    typer.echo(f"\n[{clips_table}]")
    clips = _open(conn, clips_table)
    if clips is None:
        typer.echo("  (table not found)")
    else:
        _summarize_clips(clips, sample)


if __name__ == "__main__":
    app()
