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
from pathlib import Path

import typer

from geneva_examples.core.common import connect, memory_request_bytes, setup_logging
from geneva_examples.core.config import load_config
from geneva_examples.core.utils.retry import retry_io
from geneva_examples.udfs.chunkers import VIDEO_RUNTIME_PIP, chunk_blob_video_udtf

logger = logging.getLogger(__name__)

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def run(
    config: Path | None = typer.Option(None, "--config", help="Path to config.yaml."),
    log_level: str = typer.Option("INFO", help="Logging level."),
    db_uri: str | None = typer.Option(None, help="Override config db_uri."),
    source_table: str = typer.Option("videos", help="Source videos table."),
    clips_table: str = typer.Option("video_clips", help="Output clips table."),
    openvid_uri: str = typer.Option(
        "hf://datasets/lance-format/openvid-lance/data",
        help="Base URI holding the OpenVid lance dataset (a '<table>.lance' dir).",
    ),
    openvid_table: str = typer.Option(
        "train", help="OpenVid dataset name (resolves to <uri>/<table>.lance)."
    ),
    blob_column: str = typer.Option(
        "video_blob", help="Blob column in the source dataset to read clips from."
    ),
    pointer_column: str = typer.Option(
        "openvid_rowid", help="Source-row pointer column in the videos table."
    ),
    chunk_seconds: float = typer.Option(1.0, help="Chunk length in seconds."),
    concurrency: int = typer.Option(48, help="Refresh concurrency (tasks in flight)."),
    checkpoint_size: int = typer.Option(
        32, help="Max clip rows per output fragment (commit granularity)."
    ),
    source_task_size: int | None = typer.Option(
        None,
        help="Source video rows per chunker expansion task (geneva default 1024). "
        "Smaller raises parallelism and lowers per-actor memory.",
    ),
    num_cpus: float = typer.Option(
        1.0,
        help="CPUs reserved per chunker task. concurrency*num_cpus is the total "
        "CPU demand; when it exceeds one node's cores, Ray spreads tasks across "
        "the fleet instead of packing them onto a single worker.",
    ),
    num_gpus: float = typer.Option(
        0.0,
        help="GPUs reserved per chunker task. The work is CPU-only, but a small "
        "fraction (e.g. 0.1) pins tasks onto GPU worker nodes if they are fenced "
        "to GPU-requesting tasks.",
    ),
    memory_gib: int = typer.Option(
        1, help="Memory (GiB) per chunker task (geneva caps <2)."
    ),
    max_clips: int | None = typer.Option(
        None, help="Cap clips per video (default: all)."
    ),
    max_video_s: float | None = typer.Option(
        None, help="Skip videos longer than this many seconds."
    ),
    overwrite: bool = typer.Option(
        True, help="Drop the clips table first if it already exists."
    ),
    table_write_retries: int = typer.Option(5, help="Retries for create/add ops."),
    table_write_retry_sleep_s: float = typer.Option(
        2.0, help="Base sleep (seconds) between table-write retries."
    ),
    read_retries: int = typer.Option(
        4, help="Per-row attempts to read a video's blob from the source dataset."
    ),
    read_retry_sleep_s: float = typer.Option(
        1.0, help="Base sleep (seconds) for blob-read backoff (doubles per retry)."
    ),
) -> None:
    """Chunk the videos table into a standalone clips table (1s clips)."""
    setup_logging(log_level)
    os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")

    import geneva
    from geneva.manifest import GenevaManifest

    cfg = load_config(config)
    if db_uri:
        cfg.db_uri = db_uri

    logger.info("geneva_version %s", geneva.__version__)
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
        memory_bytes=memory_request_bytes(memory_gib),
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
    view.refresh(
        concurrency=concurrency,
        max_rows_per_fragment=checkpoint_size,
        source_task_size=source_task_size,
    )
    view.checkout_latest()

    logger.info("chunk_rows %s", view.count_rows())
    logger.info("clips_table_columns %s", view.schema.names)
    logger.info(
        "clips_sample %s",
        view.search()
        .select(["video_id", "chunk_id", "start_sec", "end_sec"])
        .limit(5)
        .to_list(),
    )
    logger.info("chunk_videos_openvid_ok")


if __name__ == "__main__":
    app()
