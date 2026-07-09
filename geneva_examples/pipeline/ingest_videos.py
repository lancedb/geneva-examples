"""Video ingest CLI: download MP4s into the configured LanceDB table.

Writes a ``videos`` table with a ``video_id`` (string) and ``video``
(large_binary, raw MP4 bytes) column, ready for the chunking stage.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import typer

from geneva_examples.core.common import connect, setup_logging
from geneva_examples.core.config import load_config
from geneva_examples.core.utils.retry import retry_io

logger = logging.getLogger(__name__)

# (video_id, url) pairs ingested by default.
VIDEOS: list[tuple[str, str]] = [
    (
        "big-buck-bunny",
        "https://download.blender.org/peach/bigbuckbunny_movies/BigBuckBunny_640x360.m4v",
    ),
    (
        "sintel",
        "https://archive.org/download/Sintel/sintel-2048-stereo.mp4",
    ),
]

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def run(
    config: Path | None = typer.Option(None, "--config", help="Path to config.yaml."),
    log_level: str = typer.Option("INFO", help="Logging level."),
    db_uri: str | None = typer.Option(None, help="Override config db_uri."),
    table_name: str = typer.Option("videos", help="Target table name."),
    cache_dir: str = typer.Option(
        "./video_cache", help="Directory to cache downloaded videos."
    ),
    frag_size: int = typer.Option(
        1, help="Videos per record batch (1 = one fragment per video)."
    ),
    overwrite: bool = typer.Option(
        True, help="Drop the table first if it already exists."
    ),
    table_write_retries: int = typer.Option(5, help="Retries for create/add ops."),
    table_write_retry_sleep_s: float = typer.Option(
        2.0, help="Base sleep (seconds) between table-write retries."
    ),
) -> None:
    """Download the configured videos and load them into the table."""
    setup_logging(log_level)
    os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")
    os.environ.setdefault("HF_HOME", "./huggingface_cache")

    import geneva

    from geneva_examples.core.utils.videos import download_video_batches

    cfg = load_config(config)
    if db_uri:
        cfg.db_uri = db_uri

    logger.info("geneva_version %s", geneva.__version__)
    logger.info("db_uri %s table %s", cfg.db_uri, table_name)
    logger.info("videos %s", [vid for vid, _ in VIDEOS])

    conn = connect(cfg)

    video_batches = download_video_batches(
        VIDEOS, cache_dir=cache_dir, frag_size=frag_size
    )
    if not video_batches:
        raise RuntimeError("no videos downloaded")

    if overwrite:
        try:
            conn.drop_table(table_name)
            logger.info("dropped_existing_table %s", table_name)
        except Exception:  # noqa: BLE001
            pass

    table = retry_io(
        "create_table",
        lambda: conn.create_table(table_name, data=video_batches[0]),
        attempts=table_write_retries,
        sleep_s=table_write_retry_sleep_s,
    )
    total_batches = len(video_batches)
    for batch_index, batch in enumerate(video_batches[1:], start=2):
        retry_io(
            f"add_batch_{batch_index}",
            lambda batch=batch: table.add(batch),
            attempts=table_write_retries,
            sleep_s=table_write_retry_sleep_s,
        )
        if batch_index % 50 == 0 or batch_index == total_batches:
            logger.info("batches_loaded %d of %d", batch_index, total_batches)

    logger.info("rows_created %s", table.count_rows())
    try:
        logger.info("table_names %s", conn.table_names())
    except Exception:  # noqa: BLE001
        pass
    logger.info(
        "initial_sample %s",
        table.search().select(["video_id"]).limit(5).to_list(),
    )
    logger.info("ingest_videos_ok")


if __name__ == "__main__":
    app()
