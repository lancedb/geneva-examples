"""External-storage reference ingest CLI: register native video files as pointers.

Enumerates an S3-compatible bucket of raw video files and writes a
*reference-only* ``videos`` table — ``video_id`` + ``video_uri`` (the
``s3://bucket/key`` path) + ``size_mb`` — with **no bytes ingested**. The
:func:`chunk_uri_video_udtf` chunker later opens each URI directly on the worker.

This is the right ingest when the corpus already lives as native files in a
**separate bucket** from the LanceDB tables: the LanceDB token writes this tiny
pointer table, and only the *video* credentials (below) need read access to the
video bucket. Nothing heavy moves through the client — ingest is seconds.

Video-bucket credentials come from the ``--video-*`` options, each falling back
to the matching ``s3_*`` storage setting in ``config.yaml`` — the corpus usually
shares the object store with the LanceDB tables (same endpoint and token, just a
different bucket). Pass explicit flags when the videos sit under a *different,
bucket-scoped* token. Only the bucket name has no config equivalent and is
always passed via ``--video-bucket``.
"""

from __future__ import annotations

import logging
import os

from geneva_examples.core.common import connect, format_sample
from geneva_examples.core.config import Config
from geneva_examples.core.utils.retry import retry_io

logger = logging.getLogger(__name__)


def _endpoint_and_scheme(
    endpoint: str, default_scheme: str = "https"
) -> tuple[str, str]:
    """Split an endpoint into (host[:port], scheme) for pyarrow S3FileSystem.

    ``endpoint_override`` wants a bare host; accept a full URL and peel the
    scheme. A bare host gets ``default_scheme`` (callers pass ``http`` when the
    config says ``aws_allow_http``).
    """
    if endpoint.startswith("https://"):
        return endpoint[len("https://") :].rstrip("/"), "https"
    if endpoint.startswith("http://"):
        return endpoint[len("http://") :].rstrip("/"), "http"
    return endpoint.rstrip("/"), default_scheme


def _resolve_video_creds(
    cfg: Config,
    *,
    bucket: str = "",
    endpoint: str,
    access_key: str,
    secret_key: str,
    region: str,
    require_bucket: bool = True,
) -> tuple[str, str, str, str, str]:
    """Fill blanks from the config's ``s3_*`` storage settings; error on gaps.

    Explicit ``--video-*`` flags win so a corpus under a different, bucket-scoped
    token still works; with no flags, the same ``config.yaml`` ``s3_*`` block
    that backs the LanceDB connection is used. The chunk CLI passes
    ``require_bucket=False`` — its ``video_uri`` rows already carry the bucket.
    """
    endpoint = endpoint or cfg.s3_endpoint or ""
    access_key = access_key or cfg.s3_access_key or ""
    secret_key = secret_key or cfg.s3_secret_key or ""
    region = region or cfg.s3_region or "us-east-1"
    required = [
        ("video_bucket", bucket),
        ("video_endpoint", endpoint),
        ("video_access_key", access_key),
        ("video_secret_key", secret_key),
    ]
    if not require_bucket:
        required = required[1:]
    missing = [name for name, val in required if not val]
    if missing:
        raise RuntimeError(
            "missing video-bucket credentials (pass --video-* or set s3_* in "
            "config.yaml): " + ", ".join(missing)
        )
    return bucket, endpoint, access_key, secret_key, region


def run(
    cfg: Config,
    *,
    table_name: str = "videos",
    video_bucket: str = "",
    video_endpoint: str = "",
    video_access_key: str = "",
    video_secret_key: str = "",
    video_region: str = "",
    prefix: str = "",
    suffix: str = ".mp4",
    limit: int = 100,
    smallest_first: bool = True,
    sample: str = "",
    overwrite: bool = True,
    table_write_retries: int = 5,
    table_write_retry_sleep_s: float = 2.0,
) -> None:
    """Enumerate the video bucket and write a reference-only ``videos`` table."""
    os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")

    import geneva
    import pyarrow as pa
    import pyarrow.fs as pafs

    bucket, endpoint, access_key, secret_key, region = _resolve_video_creds(
        cfg,
        bucket=video_bucket,
        endpoint=video_endpoint,
        access_key=video_access_key,
        secret_key=video_secret_key,
        region=video_region,
    )
    host, scheme = _endpoint_and_scheme(
        endpoint, default_scheme="http" if cfg.aws_allow_http else "https"
    )

    logger.info("geneva_version %s mode %s", geneva.__version__, cfg.mode)
    logger.info("db_uri %s table %s bucket %s", cfg.db_uri, table_name, bucket)

    fs = pafs.S3FileSystem(
        access_key=access_key,
        secret_key=secret_key,
        endpoint_override=host,
        region=region,
        scheme=scheme,
    )

    # List the bucket (optionally under a prefix), keep files matching `suffix`.
    root = f"{bucket}/{prefix}".rstrip("/") if prefix else bucket
    infos = [
        i
        for i in fs.get_file_info(pafs.FileSelector(root, recursive=True))
        if i.type == pafs.FileType.File and i.path.lower().endswith(suffix.lower())
    ]
    if not infos:
        raise RuntimeError(f"no {suffix} objects found under s3://{root}")
    logger.info("found %d %s objects under s3://%s", len(infos), suffix, root)

    if sample == "stride" and limit and limit < len(infos):
        # Systematic sample across the size-sorted list: pick `limit` items at even
        # rank spacing so the selection mirrors the full size distribution (median
        # pick ≈ median object). Representative pilot, not the smallest/largest tail.
        infos.sort(key=lambda i: i.size)
        n = len(infos)
        step = n / limit
        picks = [infos[min(n - 1, round(k * step))] for k in range(limit)]
        mode = "stride (representative)"
    else:
        if sample and sample != "stride":
            raise RuntimeError(f"unknown --sample {sample!r} (supported: 'stride')")
        if smallest_first:
            infos.sort(key=lambda i: i.size)
        picks = infos[: max(0, limit)] if limit else infos
        mode = "smallest-first" if smallest_first else "listing order"
    mean_mb = sum(i.size for i in picks) / len(picks) / 1e6
    logger.info(
        "selecting %d (%s); size min/mean/max %.1f/%.1f/%.1f MB",
        len(picks),
        mode,
        min(i.size for i in picks) / 1e6,
        mean_mb,
        max(i.size for i in picks) / 1e6,
    )

    rows = pa.table(
        {
            "video_id": [p.path.rsplit("/", 1)[-1].removesuffix(suffix) for p in picks],
            "video_uri": [f"s3://{p.path}" for p in picks],
            "size_mb": [round(p.size / 1e6, 3) for p in picks],
        },
        schema=pa.schema(
            [
                pa.field("video_id", pa.string()),
                pa.field("video_uri", pa.string()),
                pa.field("size_mb", pa.float64()),
            ]
        ),
    )

    conn = connect(cfg)
    if overwrite:
        try:
            conn.drop_table(table_name)
            logger.info("dropped_existing_table %s", table_name)
        except Exception:  # noqa: BLE001
            pass

    table = retry_io(
        "create_table",
        lambda: conn.create_table(table_name, data=rows),
        attempts=table_write_retries,
        sleep_s=table_write_retry_sleep_s,
    )
    logger.info("rows_created %s", table.count_rows())
    logger.info("table_columns %s", table.schema.names)
    logger.info(
        "initial_sample\n%s",
        format_sample(
            table.search()
            .select(["video_id", "video_uri", "size_mb"])
            .limit(5)
            .to_list()
        ),
    )
    logger.info("ingest_videos_external_refs_ok")
