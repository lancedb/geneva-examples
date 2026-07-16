"""Geneva chunker (UDTF) that reads each video **by URI** from an S3-compatible
object store on the worker.

This is a third byte-source variant alongside the two in
:mod:`geneva_examples.examples.video.chunkers`:

  - ``chunk_video_udtf``      — raw ``video`` bytes in an input column.
  - ``chunk_blob_video_udtf`` — bytes from a source Lance blob column (``take_blobs``).
  - :func:`chunk_uri_video_udtf` (**here**) — a lightweight ``video_uri`` string
    column; the UDF opens the object directly on the worker via
    ``pyarrow.fs.S3FileSystem``.

Use this when the videos already live as native files in a **separate bucket**
(possibly under a *different, bucket-scoped* credential) from the LanceDB tables:
the ``videos`` table stays a pure pointer (no bytes), and only the *video* token —
not the LanceDB token — needs access to the video bucket. The video credentials
are injected into the worker environment (``VIDEO_S3_*``) by the chunk CLI's
manifest ``env_vars`` (see :mod:`chunk_external_video`) and read back here.

The output schema is identical to the other chunkers, so ``video_clips`` and the
downstream frame stages are unchanged.

Self-contained (all imports/helpers nested in the closure) because this module is
**not** importable on the remote Geneva runtime — only the manifest's pip
packages are.
"""

from __future__ import annotations

import uuid
from typing import Any

# Reuse the exact pinned runtime pip set (geneva/lancedb/pylance/pyarrow/pillow/av);
# pyarrow already provides pyarrow.fs.S3FileSystem, so no extra dependency is needed.
from geneva_examples.examples.video.chunkers import VIDEO_RUNTIME_PIP  # noqa: F401

# The UDF reads its video-bucket credentials from these worker-env keys, set by the
# chunk CLI via the manifest's env_vars (see chunk_external_video):
#   VIDEO_S3_ACCESS_KEY, VIDEO_S3_SECRET_KEY, VIDEO_S3_ENDPOINT (host, required),
#   VIDEO_S3_SCHEME (http/https), VIDEO_S3_REGION
# They are used as literals inside the closure below — the marshalled UDF cannot see
# module-level globals on the remote runtime, so they can't be shared constants.


def chunk_uri_video_udtf(
    *,
    chunk_seconds: float,
    manifest: Any,
    num_cpus: float = 1.0,
    num_gpus: float = 0.0,
    memory_bytes: int = 2 * 1024**3,
    max_video_s: float | None = None,
    num_clips: int | None = None,
    max_video_mb: float | None = None,
    read_retries: int = 4,
    read_retry_sleep_s: float = 1.0,
):
    """Build the geneva chunker that reads each video's bytes from its URI.

    The UDF receives one lightweight ``video_uri`` (``s3://bucket/key.mp4``) per
    row and **streams** it on the worker with ``pyarrow.fs.S3FileSystem`` (PyAV
    reads only the byte ranges it seeks — the whole file is never held in RAM),
    authenticated from the ``VIDEO_S3_*`` worker environment. Windowing/encoding is
    identical to :func:`chunk_blob_video_udtf`; only the byte source differs.

    ``max_video_mb`` (optional) skips objects larger than the cap *before* opening
    them. With streaming this is no longer needed to avoid OOM (peak memory is
    bounded by decode buffers, not file size) — it just caps per-video time on the
    multi-GB tail. ``max_video_s`` skips long videos after probe.
    """
    import geneva
    import pyarrow as pa

    output_schema = pa.schema(
        [
            pa.field("chunk_id", pa.int32()),
            pa.field("start_sec", pa.float32()),
            pa.field("end_sec", pa.float32()),
            pa.field("clip_bytes", pa.large_binary()),
            pa.field("frame", pa.large_binary()),
        ]
    )

    cs = float(chunk_seconds)
    limit = None if max_video_s is None else float(max_video_s)
    max_clips = None if num_clips is None else int(num_clips)
    max_bytes = None if max_video_mb is None else int(max_video_mb * 1024 * 1024)
    read_attempts = max(1, int(read_retries))
    read_sleep = float(read_retry_sleep_s)
    # Captured by the closure; the opened S3 filesystem is cached per worker actor
    # and reused across rows (a module-global won't exist on the remote runtime).
    fs_cache: dict = {}

    @geneva.chunker(  # ty: ignore[call-non-callable]  # third-party stub gap
        output_schema=output_schema,
        input_columns=["video_uri"],
        # The URI feeds the chunker but is not copied onto clip rows; `video_id`
        # (selected in the source query, not an input) is inherited onto every
        # expanded row automatically.
        inherit_input_columns=False,
        num_cpus=num_cpus,
        num_gpus=num_gpus,
        memory=memory_bytes,
        version=uuid.uuid4().hex,
        manifest=manifest,
    )
    def _chunk_uri_video(video_uri: str):
        # Runs in the remote Geneva runtime; helpers are nested so they ship with
        # the marshalled function (this module is not importable remotely). Same
        # windowing/encoding as `chunk_blob_video_udtf`; only the byte source differs.
        import io
        import logging
        import os
        import time

        import av
        import pyarrow.fs as pafs
        from PIL import Image

        log = logging.getLogger("geneva.chunk_uri_video")

        def _clip_windows(duration, chunk_s, max_n=None):
            if duration <= 0 or chunk_s <= 0:
                return []
            if max_n is not None and max_n <= 0:
                return []
            windows, start = [], 0.0
            while start < duration:
                end = min(start + chunk_s, duration)
                windows.append((start, end))
                if max_n is not None and len(windows) >= max_n:
                    break
                start += chunk_s
            return windows

        def _probe_video(src):
            with src() as f, av.open(f) as c:
                s = c.streams.video[0]
                if s.duration is not None and s.time_base is not None:
                    return float(s.duration * s.time_base)
                if c.duration is not None:  # ty: ignore[unresolved-attribute]  # third-party stub gap
                    return float(c.duration) / float(av.time_base)  # ty: ignore[unresolved-attribute]  # third-party stub gap
            return 0.0

        def _encode_clip(src, start, end):
            # Stream the source container (fresh seekable handle per clip) and do
            # both jobs from it: decode the start-frame JPEG, then re-seek and
            # stream-copy (remux) the [start, end) packets into a fresh mp4. Bytes
            # are NOT read into memory — PyAV pulls only the ranges it seeks, so
            # peak memory is bounded by decode buffers, not the file size. Remux is
            # a stream-copy (no re-encode), so it needs no libx264 in ffmpeg.
            out_buf, wrote = io.BytesIO(), 0
            frame_bytes = None
            with src() as f, av.open(f) as inp:
                ins = inp.streams.video[0]
                tb = ins.time_base

                if start > 0 and tb is not None:
                    inp.seek(int(start / tb), stream=ins, backward=True)  # ty: ignore[unresolved-attribute]  # third-party stub gap
                for fr in inp.decode(ins):  # ty: ignore[unresolved-attribute]  # third-party stub gap
                    if fr.time is None or fr.time < start:
                        continue
                    img = Image.fromarray(fr.to_ndarray(format="rgb24"))
                    img.thumbnail((512, 512), Image.Resampling.LANCZOS)
                    pbuf = io.BytesIO()
                    img.save(pbuf, format="JPEG", quality=85)
                    frame_bytes = pbuf.getvalue()
                    break

                # Always re-seek to the keyframe at/before `start` before the remux:
                # the frame-decode above advanced the demuxer, so even the start=0
                # window must rewind, or the clip begins mid-GOP with no leading
                # keyframe and is undecodable. seek(0) rewinds to the first keyframe.
                if tb is not None:
                    inp.seek(int(start / tb), stream=ins, backward=True)  # ty: ignore[unresolved-attribute]  # third-party stub gap
                with av.open(out_buf, "w", format="mp4") as out:
                    try:
                        ostream = out.add_stream_from_template(ins)
                    except AttributeError:
                        ostream = out.add_stream(template=ins)  # ty: ignore[no-matching-overload]  # third-party stub gap
                    base_dts = None
                    for packet in inp.demux(ins):  # ty: ignore[unresolved-attribute]  # third-party stub gap
                        if packet.pts is None or packet.dts is None:
                            continue
                        if float(packet.pts * tb) >= end:  # ty: ignore[unsupported-operator]  # third-party stub gap
                            break
                        if base_dts is None:
                            base_dts = packet.dts
                        packet.pts -= base_dts
                        packet.dts -= base_dts
                        packet.stream = ostream
                        out.mux(packet)
                        wrote += 1
            return (out_buf.getvalue() if wrote else None), frame_bytes

        if not video_uri:
            return

        # S3-compatible filesystem, authenticated from the worker env and cached
        # per actor. `endpoint_override` targets the object store (required for a
        # non-AWS S3 service); scheme (http/https) and region come from the env too
        # (us-east-1 is a safe SigV4 default for many S3-compatible stores).
        fs = fs_cache.get("fs")
        if fs is None:
            try:
                fs = pafs.S3FileSystem(
                    access_key=os.environ["VIDEO_S3_ACCESS_KEY"],
                    secret_key=os.environ["VIDEO_S3_SECRET_KEY"],
                    endpoint_override=os.environ["VIDEO_S3_ENDPOINT"],
                    region=os.environ.get("VIDEO_S3_REGION", "us-east-1"),
                    scheme=os.environ.get("VIDEO_S3_SCHEME", "https"),
                )
            except KeyError as e:
                log.warning(
                    "missing worker video credential env %s; skipping %s", e, video_uri
                )
                return
            fs_cache["fs"] = fs

        path = video_uri.removeprefix("s3://")  # "bucket/key.mp4"

        # Optional size guard: with streaming, big files no longer OOM, so this is
        # just an opt-in cap to skip absurdly large objects (bounds per-video time).
        if max_bytes is not None:
            try:
                info = fs.get_file_info(path)
                if info.size is not None and info.size > max_bytes:
                    log.info("skip %s: %d bytes > max_video_mb", video_uri, info.size)
                    return
            except Exception as e:  # noqa: BLE001 (stat failure -> fall through to read)
                log.debug("stat failed for %s: %s", video_uri, e)

        # Stream the object directly: a fresh seekable handle per open. The whole
        # file is NOT read into RAM — PyAV pulls only the byte ranges it seeks and
        # decodes, so peak memory is bounded by decode buffers regardless of video
        # size (the multi-GB tail no longer OOMs an actor).
        def _open():
            return fs.open_input_file(path)

        # Probe duration with retries (first network contact / transient 5xx).
        dur = None
        last_err = None
        for attempt in range(read_attempts):
            try:
                dur = _probe_video(_open)
                break
            except Exception as e:  # noqa: BLE001 (transient/corrupt; retry then skip)
                last_err = e
                if attempt + 1 < read_attempts:
                    time.sleep(read_sleep * (2**attempt))
        if dur is None:
            log.warning(
                "probe_failed %s after %d attempts: %s: %s",
                video_uri,
                read_attempts,
                type(last_err).__name__,
                last_err,
            )
            return

        if limit is not None and dur > limit:
            return
        for cid, (start, end) in enumerate(_clip_windows(dur, cs, max_n=max_clips)):
            try:
                clip, frame = _encode_clip(_open, start, end)
            except Exception as e:  # noqa: BLE001 (one bad window; log + skip)
                log.warning("encode_failed %s window=%d: %s", video_uri, cid, e)
                continue
            if clip is None:
                continue
            yield {
                "chunk_id": int(cid),
                "start_sec": float(start),
                "end_sec": float(end),
                "clip_bytes": clip,
                "frame": frame,
            }

    return _chunk_uri_video
