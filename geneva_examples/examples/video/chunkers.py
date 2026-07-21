"""Geneva chunker (UDTF) factories for splitting videos into clips.

Two factories build ``@geneva.chunker``-decorated functions that expand one
video row into many clip rows (one per fixed-length window), each carrying a
re-encoded ``clip_bytes`` and a 512px JPEG of the window's first ``frame``:

  - :func:`chunk_video_udtf` reads the raw ``video`` bytes from an input column.
  - :func:`chunk_blob_video_udtf` reads each video's blob from a source Lance
    dataset via a lightweight pointer column, so the source table stays
    reference-only (metadata + pointer, no bytes).

Failures never silently drop a source row: both chunkers carry a nullable
``errors: list<string>`` column (null on clean rows, never an empty list). A
whole-video failure yields one row with null window fields and the error
message(s); a per-window failure yields the window's row with the failed value
null. Each message leads with a stable class tag (``blob_read_failed[1/4]:``,
``decode_failed:``, ``skipped:max_video_s(...)``, ...) so failures group in
SQL, and blob-read retries record one message per failed attempt. Consumers
that want only playable clips filter ``clip_bytes IS NOT NULL``; fully clean
rows are ``errors IS NULL``.

Like the UDF factories in this package, the chunkers are fully self-contained
(all imports and helpers nested in the closure) because this module is **not**
importable on the remote Geneva runtime — only the manifest's pip packages are.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from geneva_examples.core.package_specs import package_spec

# Geneva remote runtime package pins (env-overridable for targeting other builds).
# geneva/lancedb/pylance track the installed versions so the workers match the
# client's locked env; the rest stay exact-pinned for reproducible worker builds.
GENEVA_PACKAGE_SPEC = package_spec("geneva")
LANCEDB_PACKAGE_SPEC = package_spec("lancedb")
PYLANCE_PACKAGE_SPEC = package_spec("pylance")
PYARROW_PACKAGE_SPEC = os.environ.get("PYARROW_PACKAGE_SPEC", "pyarrow==23.0.0")
PILLOW_PACKAGE_SPEC = os.environ.get("PILLOW_PACKAGE_SPEC", "pillow==12.2.0")
AV_PACKAGE_SPEC = os.environ.get("AV_PACKAGE_SPEC", "av>=12,<14")

VIDEO_RUNTIME_PIP = [
    GENEVA_PACKAGE_SPEC,
    LANCEDB_PACKAGE_SPEC,
    PYLANCE_PACKAGE_SPEC,
    PYARROW_PACKAGE_SPEC,
    PILLOW_PACKAGE_SPEC,
    AV_PACKAGE_SPEC,
]


def chunk_video_udtf(
    *,
    chunk_seconds: float,
    manifest: Any,
    num_cpus: float = 1.0,
    num_gpus: float = 0.0,
    memory_bytes: int = 1024**3,
    max_video_s: float | None = None,
    num_clips: int | None = None,
):
    """Build the geneva chunker that splits a video into clips + start frames."""
    import geneva
    import pyarrow as pa

    output_schema = pa.schema(
        [
            pa.field("chunk_id", pa.int32()),
            pa.field("start_sec", pa.float32()),
            pa.field("end_sec", pa.float32()),
            pa.field("clip_bytes", pa.large_binary()),
            pa.field("frame", pa.large_binary()),
            pa.field("errors", pa.list_(pa.string())),
        ]
    )

    cs = float(chunk_seconds)
    limit = None if max_video_s is None else float(max_video_s)
    max_clips = None if num_clips is None else int(num_clips)

    @geneva.chunker(  # ty: ignore[call-non-callable]  # third-party stub gap
        output_schema=output_schema,
        input_columns=["video"],
        # Fetch `video` to run the chunker, but don't copy it onto each clip row
        # (the bytes are large). `video_id` is inherited automatically: it stays
        # in the source projection but is not a chunker input, so it carries
        # through to every expanded row without being duplicated by the UDF.
        inherit_input_columns=False,
        num_cpus=num_cpus,
        num_gpus=num_gpus,
        memory=memory_bytes,
        version=uuid.uuid4().hex,
        manifest=manifest,
    )
    def _chunk_video(video: bytes):
        # Runs in the remote Geneva runtime; helpers are nested so they ship with
        # the marshalled function (this module is not importable remotely).
        import io
        import logging

        import av
        from PIL import Image

        log = logging.getLogger("geneva.chunk_video")

        def _err(msgs, cid=None, start=None, end=None):
            # One row accounting for a failure: window fields only when the
            # failure is per-window, data columns null, messages tag-first.
            if isinstance(msgs, str):
                msgs = [msgs]
            return {
                "chunk_id": cid,
                "start_sec": start,
                "end_sec": end,
                "clip_bytes": None,
                "frame": None,
                "errors": [str(m)[:2000] for m in msgs],
            }

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

        def _probe_video(b):
            with av.open(io.BytesIO(b)) as c:
                s = c.streams.video[0]
                if s.duration is not None and s.time_base is not None:
                    return float(s.duration * s.time_base)
                if c.duration is not None:  # ty: ignore[unresolved-attribute]  # third-party stub gap
                    return float(c.duration) / float(av.time_base)  # ty: ignore[unresolved-attribute]  # third-party stub gap
            return 0.0

        def _encode_clip(b, start, end):
            # Open the source container *once per clip* and do both jobs from it:
            # decode the start-frame JPEG, then re-seek and stream-copy (remux)
            # the [start, end) packets into a fresh mp4. Previously each clip
            # opened the video twice (once to grab the frame, once to remux),
            # wrapping the full bytes in a fresh BytesIO each time — so this
            # halves the per-clip decode setup and byte-buffer copies, the bulk
            # of the chunker's transient memory churn. Remux is a stream-copy
            # (no re-encode), so it needs no libx264 in the runtime's ffmpeg.
            out_buf, wrote = io.BytesIO(), 0
            frame_bytes = None
            with av.open(io.BytesIO(b)) as inp:
                ins = inp.streams.video[0]
                tb = ins.time_base

                # 1) start-frame image: seek to the keyframe at/before `start`,
                #    decode forward to the first frame at/after `start`, then
                #    downscale (longest side 512px) and JPEG-encode it. A
                #    full-res PNG was the dominant per-row payload; CLIP/BLIP
                #    downsize to <=336px and OpenPose rescales internally, so a
                #    512px JPEG is near-lossless downstream and ~10-30x smaller
                #    — and that payload is what bounds each actor's in-memory
                #    expansion (Geneva buffers a whole 1024-row work item).
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

                # 2) clip remux: re-seek to the same keyframe (the decode above
                #    advanced the demuxer) and stream-copy packets in
                #    [start, end), rebasing timestamps to 0.
                if start > 0 and tb is not None:
                    inp.seek(int(start / tb), stream=ins, backward=True)  # ty: ignore[unresolved-attribute]  # third-party stub gap
                with av.open(out_buf, "w", format="mp4") as out:
                    # PyAV renamed this across versions; support both spellings.
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

        if video is None:
            yield _err("video_null")
            return
        try:
            dur = _probe_video(video)
        except Exception as e:  # noqa: BLE001 (corrupt bytes; error row + log)
            log.warning(
                "decode_failed (%d bytes): %s: %s", len(video), type(e).__name__, e
            )
            yield _err(f"decode_failed: {type(e).__name__}: {e}")
            return
        if dur <= 0:
            yield _err("no_duration")
            return
        if limit is not None and dur > limit:
            yield _err(f"skipped:max_video_s({dur:.1f}s)")
            return
        for cid, (start, end) in enumerate(_clip_windows(dur, cs, max_n=max_clips)):
            try:
                clip, frame = _encode_clip(video, start, end)
            except Exception as e:  # noqa: BLE001 (one bad window; error row + log)
                log.warning("encode_failed window=%d: %s", cid, e)
                yield _err(
                    f"encode_failed: {type(e).__name__}: {e}",
                    cid=int(cid),
                    start=float(start),
                    end=float(end),
                )
                continue
            yield {
                "chunk_id": int(cid),
                "start_sec": float(start),
                "end_sec": float(end),
                "clip_bytes": clip,
                "frame": frame,
                # Partial windows keep whatever decoded: a zero-packet remux is
                # `empty_window` (frame may still be present), a clip with no
                # decodable start frame is `no_start_frame`.
                "errors": (
                    ["empty_window"]
                    if clip is None
                    else (["no_start_frame"] if frame is None else None)
                ),
            }

    return _chunk_video


def chunk_blob_video_udtf(
    *,
    source_uri: str,
    blob_column: str,
    pointer_column: str,
    chunk_seconds: float,
    manifest: Any,
    num_cpus: float = 1.0,
    num_gpus: float = 0.0,
    memory_bytes: int = 1024**3,
    max_video_s: float | None = None,
    num_clips: int | None = None,
    read_retries: int = 4,
    read_retry_sleep_s: float = 1.0,
    storage_options: dict[str, str] | None = None,
):
    """Chunker that reads each video's blob from a source Lance dataset.

    Like :func:`chunk_video_udtf`, but instead of receiving the raw ``video``
    bytes as an input column, the UDF receives a lightweight ``pointer_column``
    (the source row's ``_rowid``) and reads the blob itself on the worker via
    ``dataset.take_blobs(blob_column, ids=[...])``. This is what lets the
    ``videos`` table stay reference-only (metadata + pointer, no bytes) — the
    heavy byte movement happens cluster-side here. Output schema is identical to
    :func:`chunk_video_udtf`, so the ``video_clips`` table is unchanged.

    ``storage_options`` is forwarded to ``lance.dataset`` for object-store
    sources (endpoint, credentials, retry budget). Defaults to ``None`` (lance
    reads config from the worker environment), so existing callers are
    unaffected.
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
            pa.field("errors", pa.list_(pa.string())),
        ]
    )

    cs = float(chunk_seconds)
    limit = None if max_video_s is None else float(max_video_s)
    max_clips = None if num_clips is None else int(num_clips)
    src_uri = source_uri
    blob_col = blob_column
    storage_opts = storage_options
    read_attempts = max(1, int(read_retries))
    read_sleep = float(read_retry_sleep_s)
    # Captured by the UDF closure; deserialized once per worker actor and reused
    # across rows, so the opened dataset handle is amortized (this module is
    # not importable remotely, so a module-global won't exist on the worker).
    ds_cache: dict = {}

    @geneva.chunker(  # ty: ignore[call-non-callable]  # third-party stub gap
        output_schema=output_schema,
        input_columns=[pointer_column],
        # The pointer is fetched to read the blob but not copied onto clip rows;
        # `video_id` (selected in the source query, not an input) is inherited
        # onto every expanded row automatically.
        inherit_input_columns=False,
        num_cpus=num_cpus,
        num_gpus=num_gpus,
        memory=memory_bytes,
        version=uuid.uuid4().hex,
        manifest=manifest,
    )
    def _chunk_blob_video(openvid_rowid: int):
        # Runs in the remote Geneva runtime; helpers are nested so they ship with
        # the marshalled function (this module is not importable remotely). Same
        # windowing/encoding as `chunk_video_udtf`; only the byte source differs.
        import io
        import logging
        import time

        import av
        import lance
        from PIL import Image

        log = logging.getLogger("geneva.chunk_blob_video")

        def _err(msgs, cid=None, start=None, end=None):
            # One row accounting for a failure: window fields only when the
            # failure is per-window, data columns null, messages tag-first.
            if isinstance(msgs, str):
                msgs = [msgs]
            return {
                "chunk_id": cid,
                "start_sec": start,
                "end_sec": end,
                "clip_bytes": None,
                "frame": None,
                "errors": [str(m)[:2000] for m in msgs],
            }

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

        def _probe_video(b):
            with av.open(io.BytesIO(b)) as c:
                s = c.streams.video[0]
                if s.duration is not None and s.time_base is not None:
                    return float(s.duration * s.time_base)
                if c.duration is not None:  # ty: ignore[unresolved-attribute]  # third-party stub gap
                    return float(c.duration) / float(av.time_base)  # ty: ignore[unresolved-attribute]  # third-party stub gap
            return 0.0

        def _encode_clip(b, start, end):
            # Open the source container *once per clip* and do both jobs from it:
            # decode the start-frame JPEG, then re-seek and stream-copy (remux)
            # the [start, end) packets into a fresh mp4. Previously each clip
            # opened the video twice (once to grab the frame, once to remux),
            # wrapping the full bytes in a fresh BytesIO each time — so this
            # halves the per-clip decode setup and byte-buffer copies, the bulk
            # of the chunker's transient memory churn. Remux is a stream-copy
            # (no re-encode), so it needs no libx264 in the runtime's ffmpeg.
            out_buf, wrote = io.BytesIO(), 0
            frame_bytes = None
            with av.open(io.BytesIO(b)) as inp:
                ins = inp.streams.video[0]
                tb = ins.time_base

                # 1) start-frame image: seek to the keyframe at/before `start`,
                #    decode forward to the first frame at/after `start`, then
                #    downscale (longest side 512px) and JPEG-encode it. A
                #    full-res PNG was the dominant per-row payload; CLIP/BLIP
                #    downsize to <=336px and OpenPose rescales internally, so a
                #    512px JPEG is near-lossless downstream and ~10-30x smaller
                #    — and that payload is what bounds each actor's in-memory
                #    expansion (Geneva buffers a whole 1024-row work item).
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

                # 2) clip remux: re-seek to the same keyframe (the decode above
                #    advanced the demuxer) and stream-copy packets in
                #    [start, end), rebasing timestamps to 0.
                if start > 0 and tb is not None:
                    inp.seek(int(start / tb), stream=ins, backward=True)  # ty: ignore[unresolved-attribute]  # third-party stub gap
                with av.open(out_buf, "w", format="mp4") as out:
                    # PyAV renamed this across versions; support both spellings.
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

        if openvid_rowid is None:
            yield _err("pointer_null")
            return
        rid = int(openvid_rowid)

        # Read this row's blob from the source dataset (a ranged read; bytes never
        # touch the client). Retry transient HF/network errors with exponential
        # backoff; the dataset handle is cached per worker and dropped on error so
        # the next attempt reopens. Every failure outcome is recorded on an error
        # row (and logged), never silently dropped:
        #   - missing/empty blob  -> `blob_missing` / `blob_empty` (no retry)
        #   - read failed         -> `blob_read_failed[i/N]`, one per attempt
        #   - decode failed       -> `decode_failed` (corrupt bytes)
        video = None
        attempt_errors = []
        for attempt in range(read_attempts):
            try:
                ds = ds_cache.get("ds")
                if ds is None:
                    ds = lance.dataset(src_uri, storage_options=storage_opts)
                    ds_cache["ds"] = ds
                blobs = ds.take_blobs(blob_col, ids=[rid])
                if not blobs:
                    log.debug("blob_missing rowid=%s", rid)
                    yield _err("blob_missing")
                    return
                with blobs[0] as bf:
                    if bf.size() == 0:
                        log.debug("blob_empty rowid=%s", rid)
                        yield _err("blob_empty")
                        return
                    video = bf.readall()
                break
            except Exception as e:  # noqa: BLE001 (transient read; retry/log)
                attempt_errors.append(
                    f"blob_read_failed[{attempt + 1}/{read_attempts}]: "
                    f"{type(e).__name__}: {e}"
                )
                ds_cache.pop("ds", None)
                if attempt + 1 < read_attempts:
                    time.sleep(read_sleep * (2**attempt))
        if video is None:
            log.warning(
                "blob_read_failed rowid=%s after %d attempts: %s",
                rid,
                read_attempts,
                attempt_errors[-1] if attempt_errors else "?",
            )
            yield _err(attempt_errors or ["blob_read_failed[0/0]: unknown"])
            return

        try:
            dur = _probe_video(video)
        except Exception as e:  # noqa: BLE001 (corrupt bytes; error row + log)
            log.warning(
                "decode_failed rowid=%s (%d bytes): %s: %s",
                rid,
                len(video),
                type(e).__name__,
                e,
            )
            yield _err(f"decode_failed: {type(e).__name__}: {e}")
            return

        if dur <= 0:
            yield _err("no_duration")
            return
        if limit is not None and dur > limit:
            yield _err(f"skipped:max_video_s({dur:.1f}s)")
            return
        for cid, (start, end) in enumerate(_clip_windows(dur, cs, max_n=max_clips)):
            try:
                clip, frame = _encode_clip(video, start, end)
            except Exception as e:  # noqa: BLE001 (one bad window; error row + log)
                log.warning("encode_failed rowid=%s window=%d: %s", rid, cid, e)
                yield _err(
                    f"encode_failed: {type(e).__name__}: {e}",
                    cid=int(cid),
                    start=float(start),
                    end=float(end),
                )
                continue
            yield {
                "chunk_id": int(cid),
                "start_sec": float(start),
                "end_sec": float(end),
                "clip_bytes": clip,
                "frame": frame,
                # Partial windows keep whatever decoded: a zero-packet remux is
                # `empty_window` (frame may still be present), a clip with no
                # decodable start frame is `no_start_frame`.
                "errors": (
                    ["empty_window"]
                    if clip is None
                    else (["no_start_frame"] if frame is None else None)
                ),
            }

    return _chunk_blob_video
