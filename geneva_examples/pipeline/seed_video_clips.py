"""Seed the ``video_clips`` table with N identical clip rows — cluster-written.

A test fixture for exercising the downstream frame stages (``frame_caption``,
``frame_embed``, ``frame_openpose``) without a full OpenVid chunk run. It writes
``N`` rows that are byte-identical except for a unique ``video_id`` (random UUID).

How it works (decode once locally, replicate on the cluster):

1. Pick one row from the source ``videos`` table, read its blob from the OpenVid
   dataset, and decode a single clip (a 512px JPEG ``frame`` and a 1-second
   ``clip_bytes`` mp4) **locally** — the one and only decode. This reads one
   ~8MB source video to the client; no heavy bytes are uploaded.
2. Create the target table as a plain table with ``N`` tiny skeleton rows
   (``video_id`` UUID + ``chunk_id``/``start_sec``/``end_sec``). This is the only
   client upload (~50 bytes/row).
3. **Backfill** the heavy ``frame``/``clip_bytes`` columns with a UDF that returns
   the captured bytes for every row. The Geneva **cluster** computes and writes
   all ``N`` heavy rows; the seed bytes ship to the workers exactly once (inside
   the UDF), never per row.

This avoids the trap of chunking ``N`` rows (which decodes the same video ``N``
times and buffers whole work items in actor memory). The downstream frame stages
read only ``frame``, so ``--no-include-clip-bytes`` makes the run ~16x
smaller/faster when the clip payload isn't needed.

The target ends up as a plain table (overwrites any existing ``video_clips``); to
restore the full-dataset pipeline later, re-run ``chunk-videos-openvid``.
``--seed-clip-table`` can instead reuse a clip already present in another table
(skips the local decode).
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

import typer

from geneva_examples.core.common import (
    STABLE_ROW_IDS_STORAGE_OPTIONS,
    connect,
    memory_request_bytes,
    setup_logging,
)
from geneva_examples.core.config import load_config
from geneva_examples.core.utils.retry import retry_io
from geneva_examples.pipeline.stages._runner import backfill_column
from geneva_examples.udfs.chunkers import (
    GENEVA_PACKAGE_SPEC,
    LANCEDB_PACKAGE_SPEC,
    PYARROW_PACKAGE_SPEC,
    PYLANCE_PACKAGE_SPEC,
)

logger = logging.getLogger(__name__)

app = typer.Typer(add_completion=False, help=__doc__)

# A constant-returning UDF needs only the base runtime (geneva + lance + arrow),
# no decode/model deps — a lean env that builds fast on the workers.
BASE_RUNTIME_PIP = [
    GENEVA_PACKAGE_SPEC,
    LANCEDB_PACKAGE_SPEC,
    PYLANCE_PACKAGE_SPEC,
    PYARROW_PACKAGE_SPEC,
]


def _read_blob_local(dataset_uri: str, blob_column: str, rid: int) -> bytes:
    """Read one video blob from the source Lance dataset to the client."""
    import lance

    ds = lance.dataset(dataset_uri)
    blobs = ds.take_blobs(blob_column, ids=[rid])
    if not blobs:
        raise ValueError(f"blob missing for rowid {rid}")
    with blobs[0] as bf:
        if bf.size() == 0:
            raise ValueError(f"empty blob for rowid {rid}")
        return bf.readall()


def _decode_seed_clip(video: bytes, chunk_seconds: float):
    """Decode the first ``chunk_seconds`` window into (frame_jpeg, clip_mp4, start, end).

    Mirrors ``chunk_blob_video_udtf``'s per-clip logic for the first window: a
    512px JPEG of the start frame and a stream-copied mp4 of [0, chunk_seconds).
    """
    import io

    import av
    from PIL import Image

    start = 0.0
    with av.open(io.BytesIO(video)) as c:
        s = c.streams.video[0]
        if s.duration is not None and s.time_base is not None:
            dur = float(s.duration * s.time_base)
        elif c.duration is not None:
            dur = float(c.duration) / float(av.time_base)
        else:
            dur = 0.0
    if dur <= 0:
        raise ValueError("could not probe video duration")
    end = min(float(chunk_seconds), dur)

    out_buf, wrote, frame_bytes = io.BytesIO(), 0, None
    with av.open(io.BytesIO(video)) as inp:
        ins = inp.streams.video[0]
        tb = ins.time_base
        for fr in inp.decode(ins):
            if fr.time is None or fr.time < start:
                continue
            img = Image.fromarray(fr.to_ndarray(format="rgb24"))
            img.thumbnail((512, 512), Image.Resampling.LANCZOS)
            pbuf = io.BytesIO()
            img.save(pbuf, format="JPEG", quality=85)
            frame_bytes = pbuf.getvalue()
            break
        # Re-seek to the start keyframe (decode above advanced the demuxer) and
        # stream-copy packets in [start, end) into a fresh mp4.
        if tb is not None:
            inp.seek(int(start / tb), stream=ins, backward=True)
        with av.open(out_buf, "w", format="mp4") as out:
            try:
                ostream = out.add_stream_from_template(ins)
            except AttributeError:
                ostream = out.add_stream(template=ins)
            base_dts = None
            for packet in inp.demux(ins):
                if packet.pts is None or packet.dts is None:
                    continue
                if float(packet.pts * tb) >= end:
                    break
                if base_dts is None:
                    base_dts = packet.dts
                packet.pts -= base_dts
                packet.dts -= base_dts
                packet.stream = ostream
                out.mux(packet)
                wrote += 1
    if frame_bytes is None:
        raise ValueError("could not decode a start frame")
    clip_bytes = out_buf.getvalue() if wrote else b""
    return frame_bytes, clip_bytes, start, end


def build_constant_bytes_udf(
    *,
    input_column: str,
    payload: bytes,
    manifest: object,
    num_cpus: float,
    memory_bytes: int,
    checkpoint_size: int,
    task_size: int,
):
    """Build a UDF that returns ``payload`` for every row (ignores its input).

    The bytes are captured in the closure, so geneva marshals them to the workers
    once with the UDF — not once per row. Reads ``input_column`` only to drive the
    backfill row-for-row; the value is unused.
    """
    import geneva
    import pyarrow as pa

    data = bytes(payload)

    @geneva.udf(
        data_type=pa.large_binary(),
        input_columns=[input_column],
        num_cpus=num_cpus,
        num_gpus=0.0,
        memory=memory_bytes,
        checkpoint_size=checkpoint_size,
        task_size=task_size,
        version=uuid.uuid4().hex,
        manifest=manifest,
    )
    def _constant_bytes(value: str) -> bytes:
        return data

    return _constant_bytes


@app.command()
def run(
    config: Path | None = typer.Option(None, "--config", help="Path to config.yaml."),
    log_level: str = typer.Option("INFO", help="Logging level."),
    db_uri: str | None = typer.Option(None, help="Override config db_uri."),
    clips_table: str = typer.Option("video_clips", help="Target clips table."),
    source_table: str = typer.Option("videos", help="Source videos table."),
    num_rows: int = typer.Option(100_000, help="Number of identical rows to write."),
    include_clip_bytes: bool = typer.Option(
        True,
        help="Also replicate the clip_bytes column. Disable (~16x smaller/faster) "
        "if downstream only reads `frame`.",
    ),
    seed_clip_table: str | None = typer.Option(
        None,
        help="Reuse a clip from this existing clips table instead of decoding "
        "locally (must have a `frame` column).",
    ),
    source_video_id: str | None = typer.Option(
        None,
        help="Pick this source video_id as the basis (default: first source row).",
    ),
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
    chunk_seconds: float = typer.Option(1.0, help="Seed clip length in seconds."),
    read_retries: int = typer.Option(
        8, help="Attempts to read the seed blob (rides out HF rate limits)."
    ),
    read_retry_sleep_s: float = typer.Option(
        45.0, help="Base sleep (s) between seed-blob read attempts (linear backoff)."
    ),
    concurrency: int = typer.Option(16, help="Backfill concurrency."),
    task_size: int = typer.Option(1024, help="Rows per read task."),
    checkpoint_size: int = typer.Option(
        256, help="Rows per UDF __call__ / commit (bounds per-task output memory)."
    ),
    num_cpus: float = typer.Option(1.0, help="CPUs per backfill task."),
    memory_gib: int = typer.Option(
        1, help="Memory (GiB) per backfill task (geneva caps <2)."
    ),
    backfill_timeout_min: int = typer.Option(1000, help="Per-backfill timeout (min)."),
    flush_interval_s: float = typer.Option(30.0, help="Checkpoint flush interval (s)."),
    schema_wait_attempts: int = typer.Option(30, help="Schema-visibility attempts."),
    schema_wait_sleep_s: int = typer.Option(2, help="Seconds between schema checks."),
    table_write_retries: int = typer.Option(5, help="Retries for create/add ops."),
    table_write_retry_sleep_s: float = typer.Option(
        2.0, help="Base sleep (seconds) between table-write retries."
    ),
) -> None:
    """Replicate one clip into N identical rows of ``clips_table``."""
    setup_logging(log_level)
    os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")

    if num_rows < 1:
        raise typer.BadParameter("--num-rows must be at least 1")

    import geneva
    import pyarrow as pa
    from geneva.manifest import GenevaManifest

    cfg = load_config(config)
    if db_uri:
        cfg.db_uri = db_uri

    logger.info("geneva_version %s", geneva.__version__)
    logger.info(
        "db_uri %s clips %s num_rows %d include_clip_bytes %s",
        cfg.db_uri,
        clips_table,
        num_rows,
        include_clip_bytes,
    )

    conn = connect(cfg)

    # 1) Obtain one seed clip's bytes into client memory.
    if seed_clip_table:
        # Reuse a clip already materialized in another table.
        seed_cols = ["frame", "chunk_id", "start_sec", "end_sec"]
        if include_clip_bytes:
            seed_cols.append("clip_bytes")
        try:
            seed_src = conn.open_table(seed_clip_table)
            seed_rows = seed_src.search(None).select(seed_cols).limit(1).to_list()
        except Exception as exc:
            logger.error("could_not_read_seed_table %s: %s", seed_clip_table, exc)
            raise typer.Exit(code=1) from exc
        if not seed_rows or not seed_rows[0].get("frame"):
            logger.error("no_seed_clip in %s", seed_clip_table)
            raise typer.Exit(code=1)
        seed = seed_rows[0]
        frame_bytes = bytes(seed["frame"])
        clip_bytes = (
            bytes(seed["clip_bytes"])
            if include_clip_bytes and seed.get("clip_bytes")
            else None
        )
        chunk_id = int(seed.get("chunk_id") or 0)
        start_sec = float(seed.get("start_sec") or 0.0)
        end_sec = float(seed.get("end_sec") or 0.0)
    else:
        # Self-contained: decode one source video locally.
        if cfg.hf_token:
            os.environ.setdefault("HF_TOKEN", cfg.hf_token)
        os.environ.setdefault("HF_HOME", "./huggingface_cache")
        src = conn.open_table(source_table)
        query = src.search(None)
        if source_video_id is not None:
            escaped = source_video_id.replace("'", "''")
            query = query.where(f"video_id = '{escaped}'")
        rows = query.select(["video_id", pointer_column]).limit(1).to_list()
        if not rows:
            hint = (
                f"video_id '{source_video_id}' not found"
                if source_video_id is not None
                else f"source table {source_table} is empty"
            )
            logger.error("no_source_row: %s", hint)
            raise typer.Exit(code=1)
        rid = int(rows[0][pointer_column])
        logger.info(
            "basis_row video_id=%s %s=%d", rows[0].get("video_id"), pointer_column, rid
        )
        dataset_uri = f"{openvid_uri.rstrip('/')}/{openvid_table}.lance"
        try:
            # The blob read hits HF, which is rate-limited (shared quota); retry
            # with backoff so a single seed read rides out a busy window.
            video = retry_io(
                "read_seed_blob",
                lambda: _read_blob_local(dataset_uri, blob_column, rid),
                attempts=read_retries,
                sleep_s=read_retry_sleep_s,
            )
            frame_bytes, clip_full, start_sec, end_sec = _decode_seed_clip(
                video, chunk_seconds
            )
        except Exception as exc:
            logger.error(
                "seed_decode_failed (%s=%d): %s; try a different --source-video-id",
                pointer_column,
                rid,
                exc,
            )
            raise typer.Exit(code=1) from exc
        chunk_id = 0
        clip_bytes = clip_full if (include_clip_bytes and clip_full) else None

    logger.info(
        "seed_clip frame=%dB clip_bytes=%dB chunk_id=%d [%.2f, %.2f]",
        len(frame_bytes),
        len(clip_bytes) if clip_bytes else 0,
        chunk_id,
        start_sec,
        end_sec,
    )

    # 2) Build the N-row skeleton (tiny columns only) and overwrite the target.
    skeleton = pa.table(
        {
            "video_id": pa.array(
                [str(uuid.uuid4()) for _ in range(num_rows)], type=pa.string()
            ),
            "chunk_id": pa.array([chunk_id] * num_rows, type=pa.int32()),
            "start_sec": pa.array([start_sec] * num_rows, type=pa.float32()),
            "end_sec": pa.array([end_sec] * num_rows, type=pa.float32()),
        }
    )
    try:
        conn.drop_table(clips_table)
        logger.info("dropped_existing_table %s", clips_table)
    except Exception:  # noqa: BLE001
        pass
    retry_io(
        "create_skeleton",
        lambda: conn.create_table(
            clips_table,
            data=skeleton,
            storage_options=STABLE_ROW_IDS_STORAGE_OPTIONS,
        ),
        attempts=table_write_retries,
        sleep_s=table_write_retry_sleep_s,
    )
    # Re-open fresh: a handle straight from create_table can carry a stale schema
    # version for a reused table name on remote (db://) dispatch, which makes the
    # subsequent add_columns see phantom old columns. Opening anew binds to the
    # committed skeleton version (mirrors the stage CLIs: open_table then backfill).
    table = conn.open_table(clips_table)
    table.checkout_latest()
    logger.info(
        "skeleton_rows %d cols %s -> %s",
        table.count_rows(),
        table.schema.names,
        clips_table,
    )

    # 3) Backfill the heavy columns with constant-returning UDFs (cluster-side).
    #    Stable manifest name so the worker env is reused across runs.
    manifest = (
        GenevaManifest.create_pip("seed-video-clips-rt").pip(BASE_RUNTIME_PIP).build()
    )
    memory_bytes = memory_request_bytes(memory_gib)

    def _do_backfill(column: str, payload: bytes) -> None:
        udf = build_constant_bytes_udf(
            input_column="video_id",
            payload=payload,
            manifest=manifest,
            num_cpus=num_cpus,
            memory_bytes=memory_bytes,
            checkpoint_size=checkpoint_size,
            task_size=task_size,
        )
        nonlocal table
        table = backfill_column(
            conn=conn,
            table=table,
            table_name=clips_table,
            column=column,
            udf=udf,
            concurrency=concurrency,
            task_size=task_size,
            checkpoint_size=checkpoint_size,
            flush_interval_s=flush_interval_s,
            timeout_min=backfill_timeout_min,
            wait_attempts=schema_wait_attempts,
            wait_sleep_s=schema_wait_sleep_s,
        )

    _do_backfill("frame", frame_bytes)
    if clip_bytes is not None:
        _do_backfill("clip_bytes", clip_bytes)

    logger.info("clips_rows %s", table.count_rows())
    logger.info("clips_table_columns %s", table.schema.names)
    logger.info(
        "clips_sample %s",
        table.search()
        .select(["video_id", "chunk_id", "start_sec", "end_sec"])
        .limit(5)
        .to_list(),
    )
    logger.info("seed_video_clips_ok")


if __name__ == "__main__":
    app()
