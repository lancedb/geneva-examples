"""Video chunking CLI: split videos into fixed-length clips + a start frame.

A geneva ``chunker`` UDTF splits each row of the ``videos`` table into
``chunk-seconds`` windows, emitting one row per window with the re-encoded clip
(``clip_bytes``) and a 512px JPEG of the window's first frame (``frame``). Geneva only
runs a chunker inside a materialized view, so that view *is* the output
``video_clips`` table: it's created under ``--clips-table`` and refreshed in
place — no intermediary ``_mv`` table and no full in-memory copy.

The raw ``video`` bytes are fed to the UDF via ``input_columns`` with
``inherit_input_columns=False``, so they are fetched to run the chunker but
never written onto clip rows — the (large) video is never duplicated per clip.
``video_id`` is carried through automatically: it stays in the source
projection but is not a chunker input, so geneva inherits it onto every clip.

The chunker factories themselves live in :mod:`geneva_examples.examples.video.chunkers`, beside the
UDF factories, so they can be reused (e.g. by UDF Studio) independently of this
CLI.
"""

from __future__ import annotations

import logging
import os

from geneva_examples.core.common import (
    build_manifest,
    connect,
    format_sample,
    local_concurrency,
    resolve_resources,
    runtime_session,
    unique_cluster_name,
)
from geneva_examples.core.config import Config
from geneva_examples.core.utils.retry import retry_io
from geneva_examples.examples.video.chunkers import VIDEO_RUNTIME_PIP, chunk_video_udtf

logger = logging.getLogger(__name__)


def run(
    cfg: Config,
    *,
    source_table: str = "videos",
    clips_table: str = "video_clips",
    chunk_seconds: float = 10.0,
    concurrency: int = 2,
    checkpoint_size: int = 8,
    source_task_size: int | None = None,
    num_cpus: float = 1.0,
    memory_gib: int = 1,
    max_clips: int | None = None,
    max_video_s: float | None = None,
    overwrite: bool = True,
    table_write_retries: int = 5,
    table_write_retry_sleep_s: float = 2.0,
) -> None:
    """Chunk the videos table into a standalone clips table."""
    os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")

    import geneva

    num_cpus, _, memory_bytes = resolve_resources(
        cfg, num_cpus=num_cpus, num_gpus=None, memory_gib=memory_gib
    )

    logger.info("geneva_version %s mode %s", geneva.__version__, cfg.mode)
    logger.info(
        "db_uri %s source %s clips %s chunk_seconds %s",
        cfg.db_uri,
        source_table,
        clips_table,
        chunk_seconds,
    )

    conn = connect(cfg)
    src = conn.open_table(source_table)

    if overwrite:
        try:
            conn.drop_table(clips_table)
            logger.info("dropped_existing_table %s", clips_table)
        except Exception:  # noqa: BLE001
            pass

    manifest = build_manifest(cfg, "video-chunking", VIDEO_RUNTIME_PIP)
    udtf = chunk_video_udtf(
        chunk_seconds=chunk_seconds,
        manifest=manifest,
        num_cpus=num_cpus,
        memory_bytes=memory_bytes,
        max_video_s=max_video_s,
        num_clips=max_clips,
    )

    # `video` must be selected here to feed the UDF (the chunker validates its
    # input_columns against the source query's projection, server-side), and
    # `video_id` is selected so geneva inherits it onto the clip rows. Because
    # the chunker sets inherit_input_columns=False, `video` is dropped from the
    # view's output rows — so the clips table never stores the movie bytes.
    #
    # The view IS the clips table: geneva only runs the chunker inside a
    # materialized view, so we create it under `clips_table` directly and refresh
    # in place — no separate `_mv` table and no in-memory copy of every clip.
    view = retry_io(
        "create_clips_view",
        lambda: conn.create_udtf_view(
            clips_table,
            source=src.search(None).select(["video_id", "video"]),
            udtf=udtf,
        ),
        attempts=table_write_retries,
        sleep_s=table_write_retry_sleep_s,
    )
    refresh_kwargs: dict = {}
    if cfg.is_local:
        concurrency = local_concurrency(concurrency)
        refresh_kwargs["_admission_check"] = False
    else:
        # Unique per-job cluster name so concurrent jobs don't collide on the
        # fixed default under per-job ephemeral RayClusters (ignored locally).
        refresh_kwargs["cluster"] = unique_cluster_name(clips_table)
    with runtime_session(conn, cfg):
        view.refresh(
            concurrency=concurrency,
            max_rows_per_fragment=checkpoint_size,
            source_task_size=source_task_size,
            **refresh_kwargs,
        )
    view.checkout_latest()

    logger.info("chunk_rows %s", view.count_rows())
    logger.info("clips_table_columns %s", view.schema.names)
    logger.info(
        "clips_sample\n%s",
        format_sample(
            view.search()
            .select(["video_id", "chunk_id", "start_sec", "end_sec"])
            .limit(5)
            .to_list()
        ),
    )
    logger.info("chunk_videos_ok")
