"""Chunk short OpenVid videos into 1-second clips.

Geneva ``chunker`` pipeline tuned for OpenVid's many short (~few-second, ~8 MB)
clips: 1-second windows, higher refresh concurrency, and larger output fragments.

Unlike the movie chunker, the ``videos`` table here is *reference-only* (metadata
+ an ``openvid_rowid`` pointer, no bytes — see ``ingest-videos-openvid``).
This uses ``chunk_blob_video_udtf``, which reads each video's blob directly from
the source OpenVid Lance dataset on the worker via ``take_blobs`` — so the raw
bytes never transit the client, just the cluster-side decode.

The UDTF view *is* the output table: geneva can only execute a chunker inside a
materialized view, so we create that view directly under ``--clips-table`` and
refresh it in place — no intermediary ``_mv`` table and no full in-memory copy.
``video_clips`` still stores fully materialized ``clip_bytes``/``frame``, so
downstream frame stages are unchanged. Because each clips table is a view bound
to one source + chunker, OpenVid and movie clips can't share one table — point
``--clips-table`` at distinct names to keep both.
"""

from __future__ import annotations

import logging
import os
import uuid

from geneva_examples.core.common import (
    connect,
    format_sample,
    local_concurrency,
    resolve_resources,
    runtime_session,
)
from geneva_examples.core.config import Config
from geneva_examples.core.utils.retry import retry_io
from geneva_examples.examples.video.chunkers import (
    VIDEO_RUNTIME_PIP,
    chunk_blob_video_udtf,
)

logger = logging.getLogger(__name__)


def run(
    cfg: Config,
    *,
    source_table: str = "videos",
    clips_table: str = "video_clips",
    openvid_uri: str = "hf://datasets/lance-format/openvid-lance/data",
    openvid_table: str = "train",
    blob_column: str = "video_blob",
    pointer_column: str = "openvid_rowid",
    chunk_seconds: float = 1.0,
    concurrency: int = 4,
    checkpoint_size: int = 32,
    source_task_size: int | None = None,
    num_cpus: float = 1.0,
    num_gpus: float = 0.0,
    memory_gib: int = 1,
    max_clips: int | None = None,
    max_video_s: float | None = None,
    overwrite: bool = True,
    table_write_retries: int = 5,
    table_write_retry_sleep_s: float = 2.0,
    read_retries: int = 4,
    read_retry_sleep_s: float = 1.0,
) -> None:
    """Chunk the videos table into a standalone clips table (1s clips)."""
    os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")

    import geneva

    num_cpus, num_gpus, memory_bytes = resolve_resources(
        cfg, num_cpus=num_cpus, num_gpus=num_gpus, memory_gib=memory_gib
    )

    logger.info("geneva_version %s mode %s", geneva.__version__, cfg.mode)
    logger.info(
        "db_uri %s source %s clips %s chunk_seconds %s",
        cfg.db_uri,
        source_table,
        clips_table,
        chunk_seconds,
    )
    logger.info(
        "scheduling concurrency=%d num_cpus=%.2f num_gpus=%.2f "
        "-> total_cpu_demand=%.1f (spreads across the fleet once this exceeds "
        "one worker's cores)",
        concurrency,
        num_cpus,
        num_gpus,
        concurrency * num_cpus,
    )

    conn = connect(cfg)
    src = conn.open_table(source_table)

    if overwrite:
        try:
            conn.drop_table(clips_table)
            logger.info("dropped_existing_table %s", clips_table)
        except Exception:  # noqa: BLE001
            pass

    # huggingface-hub lets workers resolve the `hf://` source; HF_HOME points the
    # cache at a writable worker path. A token (when configured) moves the workers
    # off the shared per-IP anonymous rate limit onto the authenticated quota —
    # important when many workers read from HF concurrently.
    worker_env = {"HF_HOME": "/tmp/hf_cache"}
    if cfg.hf_token:
        worker_env["HF_TOKEN"] = cfg.hf_token
        logger.info("hf_token present; workers will authenticate to HF")
    else:
        logger.info("no hf_token configured; workers read HF anonymously")

    if cfg.is_local:
        # Local Ray workers share the driver's environment, so there is no remote
        # manifest to attach `env_vars` to — set the HF vars here in-process.
        for key, value in worker_env.items():
            os.environ.setdefault(key, value)
        manifest = None
    else:
        from geneva.manifest import GenevaManifest

        manifest = (
            GenevaManifest.create_pip(f"video-chunking-{uuid.uuid4().hex[:6]}")
            .pip([*VIDEO_RUNTIME_PIP, "huggingface-hub>=0.24"])
            .env_vars(worker_env)
            .build()
        )
    dataset_uri = f"{openvid_uri.rstrip('/')}/{openvid_table}.lance"
    logger.info("source_dataset %s blob_column %s", dataset_uri, blob_column)
    udtf = chunk_blob_video_udtf(
        source_uri=dataset_uri,
        blob_column=blob_column,
        pointer_column=pointer_column,
        chunk_seconds=chunk_seconds,
        manifest=manifest,
        num_cpus=num_cpus,
        num_gpus=num_gpus,
        memory_bytes=memory_bytes,
        max_video_s=max_video_s,
        num_clips=max_clips,
        read_retries=read_retries,
        read_retry_sleep_s=read_retry_sleep_s,
    )

    # `pointer_column` feeds the UDF (it reads the blob from the source dataset on
    # the worker); `video_id` is selected so geneva inherits it onto each clip
    # row. With inherit_input_columns=False, the pointer is dropped from the view
    # output, and the OpenVid metadata columns in `videos` are simply not selected
    # here, so they stay in the source table (joinable on `video_id`).
    #
    # The view IS the clips table: geneva only runs the chunker inside a
    # materialized view, so we create it under `clips_table` directly and refresh
    # in place — no separate `_mv` table and no in-memory copy of every clip.
    view = retry_io(
        "create_clips_view",
        lambda: conn.create_udtf_view(
            clips_table,
            source=src.search(None).select(["video_id", pointer_column]),
            udtf=udtf,
        ),
        attempts=table_write_retries,
        sleep_s=table_write_retry_sleep_s,
    )
    refresh_kwargs: dict = {}
    if cfg.is_local:
        concurrency = local_concurrency(concurrency)
        refresh_kwargs["_admission_check"] = False
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
    logger.info("chunk_videos_openvid_ok")
