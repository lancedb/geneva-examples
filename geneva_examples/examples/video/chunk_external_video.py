"""Chunk a reference-only ``videos`` table whose rows are external object URIs.

Pairs with :mod:`ingest_external_refs`. The ``videos`` table holds a ``video_uri``
pointer per row (no bytes); this step runs :func:`chunk_uri_video_udtf`, which
opens each URI directly on the worker via ``pyarrow.fs.S3FileSystem`` and emits
``chunk-seconds`` clips (``clip_bytes`` + a 512px start ``frame``) into a
``video_clips`` table.

The UDTF view *is* the output table: geneva only runs a chunker inside a
materialized view, so we create it under ``--clips-table`` and refresh in place.

Video-bucket credentials (``--video-*`` flags, falling back to the ``s3_*``
storage settings in ``config.yaml``) are injected into the worker environment as
``VIDEO_S3_*`` via the manifest's ``env_vars`` — the *only* token that needs
read access to the video bucket. Pass explicit flags when the corpus sits under
a different, bucket-scoped token than the LanceDB tables.

``source_task_size=1`` (the default here) puts one video per expansion task so
the work fans out across the fleet — essential for parallel video decode.
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
from geneva_examples.examples.video.chunkers import VIDEO_RUNTIME_PIP
from geneva_examples.examples.video.chunkers_uri import chunk_uri_video_udtf
from geneva_examples.examples.video.ingest_external_refs import (
    _endpoint_and_scheme,
    _resolve_video_creds,
)

logger = logging.getLogger(__name__)


def run(
    cfg: Config,
    *,
    source_table: str = "videos",
    clips_table: str = "video_clips",
    uri_column: str = "video_uri",
    video_endpoint: str = "",
    video_access_key: str = "",
    video_secret_key: str = "",
    video_region: str = "",
    chunk_seconds: float = 10.0,
    concurrency: int = 8,
    checkpoint_size: int = 32,
    source_task_size: int | None = 1,
    num_cpus: float = 1.0,
    num_gpus: float = 0.0,
    memory_gib: int = 2,
    max_clips: int | None = None,
    max_video_s: float | None = None,
    max_video_mb: float | None = None,
    read_retries: int = 4,
    read_retry_sleep_s: float = 1.0,
    detach: bool = False,
    overwrite: bool = True,
    table_write_retries: int = 5,
    table_write_retry_sleep_s: float = 2.0,
) -> None:
    """Chunk the URI-pointer ``videos`` table into a standalone clips table."""
    os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")

    import geneva

    # Video-bucket creds (bucket itself isn't needed here — the URIs carry it).
    _, endpoint, access_key, secret_key, region = _resolve_video_creds(
        cfg,
        endpoint=video_endpoint,
        access_key=video_access_key,
        secret_key=video_secret_key,
        region=video_region,
        require_bucket=False,
    )
    host, scheme = _endpoint_and_scheme(
        endpoint, default_scheme="http" if cfg.aws_allow_http else "https"
    )

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
        "scheduling concurrency=%d num_cpus=%.2f source_task_size=%s "
        "-> total_cpu_demand=%.1f",
        concurrency,
        num_cpus,
        source_task_size,
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

    # The video token reaches the workers via the manifest env. The endpoint is
    # split into a bare host + scheme so the UDF's S3FileSystem can target a
    # non-AWS S3 service on either http or https (endpoint_override wants a host).
    worker_env = {
        "VIDEO_S3_ACCESS_KEY": access_key,
        "VIDEO_S3_SECRET_KEY": secret_key,
        "VIDEO_S3_ENDPOINT": host,
        "VIDEO_S3_SCHEME": scheme,
        "VIDEO_S3_REGION": region,
    }
    if cfg.is_local:
        # Local Ray workers share the driver env; no remote manifest to attach
        # to. Overwrite rather than setdefault: the resolved flag/config values
        # must win over any stale ambient VIDEO_S3_* so the UDF sees exactly
        # what this run was asked to use.
        os.environ.update(worker_env)
        manifest = None
    else:
        from geneva.manifest import GenevaManifest

        manifest = (
            GenevaManifest.create_pip(f"chunk-external-{uuid.uuid4().hex[:6]}")
            .pip([*VIDEO_RUNTIME_PIP])
            .env_vars(worker_env)
            .build()
        )

    udtf = chunk_uri_video_udtf(
        chunk_seconds=chunk_seconds,
        manifest=manifest,
        num_cpus=num_cpus,
        num_gpus=num_gpus,
        memory_bytes=memory_bytes,
        max_video_s=max_video_s,
        num_clips=max_clips,
        max_video_mb=max_video_mb,
        read_retries=read_retries,
        read_retry_sleep_s=read_retry_sleep_s,
    )

    # `uri_column` feeds the UDF; `video_id` is selected so geneva inherits it onto
    # each clip row. With inherit_input_columns=False the pointer is dropped from
    # the view output, so the clips table stores only the clip payload + video_id.
    view = retry_io(
        "create_clips_view",
        lambda: conn.create_udtf_view(
            clips_table,
            source=src.search(None).select(["video_id", uri_column]),
            udtf=udtf,
        ),
        attempts=table_write_retries,
        sleep_s=table_write_retry_sleep_s,
    )
    refresh_kwargs: dict = {}
    if cfg.is_local:
        concurrency = local_concurrency(concurrency)
        refresh_kwargs["_admission_check"] = False

    if detach and cfg.is_local:
        # A detached local refresh runs in the driver's Ray, which
        # runtime_session tears down on exit — the job would be killed. Only
        # remote/enterprise refreshes truly run decoupled on the cluster.
        logger.warning(
            "--detach is ignored in local mode (local Ray is torn down on exit); "
            "running synchronously"
        )
        detach = False

    if detach:
        # Fire-and-forget: submit the refresh and return its job id without
        # waiting. The job runs on the cluster driver pod; monitor it with
        # `uv run jobs tail <job-id>` / `conn.get_job(<job-id>)`.
        job = view.refresh_async(
            concurrency=concurrency,
            max_rows_per_fragment=checkpoint_size,
            source_task_size=source_task_size,
            **refresh_kwargs,
        )
        logger.info("submitted detached refresh; job_id %s", job.job_id)
        logger.info("monitor with: uv run jobs tail %s", job.job_id)
        logger.info("chunk_videos_external_submitted")
        return

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
    logger.info("chunk_videos_external_ok")
