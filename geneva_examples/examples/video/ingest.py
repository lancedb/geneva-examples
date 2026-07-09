"""Video ingest CLI: download MP4s into the configured LanceDB table.

Writes a ``videos`` table with a ``video_id`` (string) and ``video``
(large_binary, raw MP4 bytes) column, ready for the chunking stage.
"""

from __future__ import annotations

import logging
import os

from geneva_examples.core.common import connect, format_sample
from geneva_examples.core.config import Config
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


def run(
    cfg: Config,
    *,
    table_name: str = "videos",
    cache_dir: str = "./video_cache",
    frag_size: int = 1,
    overwrite: bool = True,
    table_write_retries: int = 5,
    table_write_retry_sleep_s: float = 2.0,
) -> None:
    """Download the configured videos and load them into the table."""
    os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")
    os.environ.setdefault("HF_HOME", "./huggingface_cache")

    import geneva

    from geneva_examples.core.utils.videos import download_video_batches

    logger.info("geneva_version %s mode %s", geneva.__version__, cfg.mode)
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
        "initial_sample\n%s",
        format_sample(table.search().select(["video_id"]).limit(5).to_list()),
    )
    logger.info("ingest_videos_ok")
