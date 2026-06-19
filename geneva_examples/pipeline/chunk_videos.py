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

The chunker factories themselves live in :mod:`geneva_examples.udfs.chunkers`, beside the
UDF factories, so they can be reused (e.g. by UDF Studio) independently of this
CLI.
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
from geneva_examples.udfs.chunkers import VIDEO_RUNTIME_PIP, chunk_video_udtf

logger = logging.getLogger(__name__)

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def run(
    config: Path | None = typer.Option(None, "--config", help="Path to config.yaml."),
    log_level: str = typer.Option("INFO", help="Logging level."),
    db_uri: str | None = typer.Option(None, help="Override config db_uri."),
    source_table: str = typer.Option("videos", help="Source videos table."),
    clips_table: str = typer.Option("video_clips", help="Output clips table."),
    chunk_seconds: float = typer.Option(10.0, help="Chunk length in seconds."),
    concurrency: int = typer.Option(2, help="Refresh concurrency."),
    checkpoint_size: int = typer.Option(
        8, help="Max clip rows per output fragment (commit granularity)."
    ),
    source_task_size: int | None = typer.Option(
        None,
        help="Source video rows per chunker expansion task (geneva default 1024). "
        "Smaller raises parallelism and lowers per-actor memory.",
    ),
    num_cpus: float = typer.Option(1.0, help="CPUs per chunker task."),
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
) -> None:
    """Chunk the videos table into a standalone clips table."""
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

    conn = connect(cfg)
    src = conn.open_table(source_table)

    if overwrite:
        try:
            conn.drop_table(clips_table)
            logger.info("dropped_existing_table %s", clips_table)
        except Exception:  # noqa: BLE001
            pass

    manifest = (
        GenevaManifest.create_pip(f"video-chunking-{uuid.uuid4().hex[:6]}")
        .pip(VIDEO_RUNTIME_PIP)
        .build()
    )
    udtf = chunk_video_udtf(
        chunk_seconds=chunk_seconds,
        manifest=manifest,
        num_cpus=num_cpus,
        memory_bytes=memory_request_bytes(memory_gib),
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
    logger.info("chunk_videos_ok")


if __name__ == "__main__":
    app()
