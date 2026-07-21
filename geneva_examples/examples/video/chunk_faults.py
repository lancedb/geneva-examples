"""Fault-injection demo for the clips ``errors`` column.

Seeds a poisoned OpenVid-style source dataset — good, corrupt, truncated,
empty, and missing video blobs — plus a pointer table with a dangling and a
null ``openvid_rowid``, then runs the **unchanged** ``chunk-videos-openvid``
pipeline over it. Failures are injected purely via data: no chunker
modifications, no test hooks, so every error row in the resulting clips table
was produced by the same code path a real run would take.

The report at the end shows the raw clips rows, every full error message, and
an expected-vs-observed table of error classes per video. The run fails
(non-zero exit) only if a clean video grew errors or a faulty video produced
no error row; class-level differences are warnings, since the truncation
outcomes are ffmpeg-version-sensitive (pinned exactly in tests/test_udfs.py
against the locked environment).

Local-mode only by default: the poisoned dataset is written to a laptop path
that enterprise workers cannot read (pass an object-store ``--data-dir`` to
override).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from geneva_examples.core.common import connect, format_sample
from geneva_examples.core.config import Config
from geneva_examples.core.utils.retry import retry_io

logger = logging.getLogger(__name__)

_WIDTH, _HEIGHT, _FPS = 64, 48, 10

# Expected error classes per corpus video (class = message text before the
# first ':' or '['). "clean" marks rows with errors NULL. Only the *presence*
# checks are strict; exact classes for the truncated variants are pinned in
# tests against the locked ffmpeg/av build.
EXPECTED: dict[str, set[str]] = {
    "good": {"clean"},
    "empty-blob": {"blob_empty"},
    # Current lance returns a size-0 BlobFile for a null blob (same surface as
    # an empty one); `blob_missing` stays reachable for take_blobs returning []
    # (e.g. stable-row-id datasets filter unknown ids instead of raising).
    "null-blob": {"blob_empty"},
    "garbage": {"decode_failed"},
    "raw-h264": {"no_duration"},
    "trunc-gop": {"clean", "empty_window"},
    "trunc-nokey": {"clean", "no_start_frame"},
    "trunc-midpkt": {"clean", "encode_failed", "empty_window"},
    "too-long": {"skipped"},
    "dangling": {"blob_read_failed"},
    "null-pointer": {"pointer_null"},
}


def _make_mp4(
    seconds: float = 3.0,
    *,
    fps: int = _FPS,
    gop: int | None = None,
    faststart: bool = False,
) -> bytes:
    """Synthetic libx264 mp4 (64x48 gray ramp), as tests/conftest.make_mp4.

    ``gop`` sets the keyframe interval (frames); libx264's default (~250) puts a
    single keyframe at t=0 in a 3s clip, ``gop=10`` gives one per second —
    which keyframes exist decides how a truncated file fails. ``faststart``
    moves the moov atom ahead of mdat so a tail-truncated file still probes its
    full duration.
    """
    import io
    import tempfile

    import av
    import numpy as np

    def _write(target) -> None:
        # +faststart needs a real, named output file: movenc rewrites the file
        # to move moov ahead of mdat on close, which PyAV's custom-IO
        # (BytesIO) targets cannot do — it raises FileNotFoundError at close.
        options = {"movflags": "+faststart"} if faststart else {}
        stream_options = {"g": str(gop)} if gop is not None else {}
        with av.open(target, mode="w", format="mp4", options=options) as container:
            stream = container.add_stream("libx264", rate=fps, options=stream_options)
            stream.width, stream.height, stream.pix_fmt = _WIDTH, _HEIGHT, "yuv420p"
            for i in range(int(seconds * fps)):
                arr = np.full((_HEIGHT, _WIDTH, 3), (i * 8) % 255, dtype=np.uint8)
                frame = av.VideoFrame.from_ndarray(arr, format="rgb24")
                for packet in stream.encode(frame):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)

    if not faststart:
        buf = io.BytesIO()
        _write(buf)
        return buf.getvalue()
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        path = Path(tmp.name)
    try:
        _write(str(path))
        return path.read_bytes()
    finally:
        path.unlink(missing_ok=True)


def _make_raw_h264(seconds: float = 3.0, *, fps: int = _FPS) -> bytes:
    """H.264 elementary stream: no container metadata, so no probeable duration."""
    import io

    import av
    import numpy as np

    buf = io.BytesIO()
    with av.open(buf, mode="w", format="h264") as container:
        stream = container.add_stream("libx264", rate=fps)
        stream.width, stream.height, stream.pix_fmt = _WIDTH, _HEIGHT, "yuv420p"
        for i in range(int(seconds * fps)):
            arr = np.full((_HEIGHT, _WIDTH, 3), (i * 8) % 255, dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(arr, format="rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return buf.getvalue()


def _make_garbage() -> bytes:
    """64 KiB of deterministic non-video bytes (av.open raises InvalidDataError)."""
    return bytes(range(256)) * 256


def _truncate_at_packet(
    mp4: bytes, cut_sec: float, *, mid_packet: bool = False
) -> bytes:
    """Cut a faststart mp4 at the first packet with pts >= ``cut_sec``.

    Byte-percentage cuts don't work on tiny synthetic files (moov is ~half the
    bytes), so cut relative to packet positions from a demux pass. With
    ``mid_packet`` the cut lands halfway *into* that packet, leaving corrupt
    trailing data instead of a clean packet boundary.
    """
    import io

    import av

    with av.open(io.BytesIO(mp4)) as container:
        stream = container.streams.video[0]
        tb = stream.time_base
        for packet in container.demux(stream):  # ty: ignore[unresolved-attribute]  # third-party stub gap
            if packet.pts is None or packet.pos is None:
                continue
            if float(packet.pts * tb) >= cut_sec:  # ty: ignore[unsupported-operator]  # third-party stub gap
                cut = packet.pos + (packet.size // 2 if mid_packet else 0)
                return mp4[:cut]
    return mp4


def _build_corpus() -> list[tuple[str, bytes | None]]:
    """(video_id, blob) rows for the poisoned source dataset."""
    return [
        ("good", _make_mp4(3.0)),
        ("empty-blob", b""),
        ("null-blob", None),
        ("garbage", _make_garbage()),
        ("raw-h264", _make_raw_h264(3.0)),
        ("trunc-gop", _truncate_at_packet(_make_mp4(3.0, gop=10, faststart=True), 2.0)),
        ("trunc-nokey", _truncate_at_packet(_make_mp4(3.0, faststart=True), 2.0)),
        # Cutting halfway into the 1.0s keyframe packet corrupts window 1's
        # decode (encode_failed) and starves window 2 entirely (empty_window).
        (
            "trunc-midpkt",
            _truncate_at_packet(
                _make_mp4(3.0, gop=10, faststart=True), 1.0, mid_packet=True
            ),
        ),
        ("too-long", _make_mp4(6.0)),
    ]


def _error_classes(rows: list[dict]) -> set[str]:
    """Class tags observed across a video's clip rows ('clean' for null errors)."""
    tags: set[str] = set()
    for row in rows:
        errs = row.get("errors")
        if errs:
            tags |= {e.split(":")[0].split("[")[0] for e in errs}
        else:
            tags.add("clean")
    return tags


def run(
    cfg: Config,
    *,
    data_dir: str = "./faults_demo_data",
    videos_table: str = "videos_faults",
    clips_table: str = "video_clips_faults",
    chunk_seconds: float = 1.0,
    max_video_s: float = 5.0,
    read_retries: int = 2,
    read_retry_sleep_s: float = 0.2,
    concurrency: int = 2,
) -> None:
    """Seed poisoned videos, chunk them, and report the errors column."""
    import lance
    import pyarrow as pa

    from geneva_examples.examples.video import chunk_openvid

    is_object_store = "://" in data_dir
    if not cfg.is_local and not is_object_store:
        raise SystemExit(
            "chunk-videos-faults is a local-mode demo: enterprise workers cannot "
            "read a laptop path. Re-run with --mode local, or point --data-dir "
            "at object storage the cluster can reach."
        )
    base_uri = (
        data_dir.rstrip("/")
        if is_object_store
        else str(Path(data_dir).expanduser().resolve())
    )
    # The URI is baked into the UDF closure and opened inside Ray workers whose
    # cwd differs, hence the resolve() above.
    dataset_uri = f"{base_uri}/train.lance"

    # 1) Poisoned source dataset, shaped like OpenVid (blob-encoded video column).
    # NOTE: no stable row ids — with them, take_blobs on a dangling pointer is
    # silently filtered (-> blob_missing) instead of raising (-> retry history).
    corpus = _build_corpus()
    blob_field = pa.field(
        "video_blob", pa.large_binary(), metadata={"lance-encoding:blob": "true"}
    )
    schema = pa.schema([pa.field("video_id", pa.string()), blob_field])
    table = pa.table(
        {
            "video_id": [vid for vid, _ in corpus],
            "video_blob": [blob for _, blob in corpus],
        },
        schema=schema,
    )
    lance.write_dataset(table, dataset_uri, mode="overwrite")
    logger.info("poisoned_dataset %s rows=%d", dataset_uri, len(corpus))

    # 2) Pointer table: real _rowids from a scan (as ingest-videos-openvid does),
    # plus a dangling and a null pointer that have no source row at all.
    ds = lance.dataset(dataset_uri)
    pointers: list[tuple[str, int | None]] = []
    for batch in ds.scanner(columns=["video_id"], with_row_id=True).to_batches():
        pointers += list(
            zip(
                batch["video_id"].to_pylist(),
                batch["_rowid"].to_pylist(),
                strict=True,
            )
        )
    pointers += [("dangling", 999_999), ("null-pointer", None)]
    ptr_schema = pa.schema(
        [pa.field("video_id", pa.string()), pa.field("openvid_rowid", pa.int64())]
    )
    ptr_table = pa.table(
        {
            "video_id": [vid for vid, _ in pointers],
            "openvid_rowid": [rid for _, rid in pointers],
        },
        schema=ptr_schema,
    )
    conn = connect(cfg)
    try:
        conn.drop_table(videos_table)
        logger.info("dropped_existing_table %s", videos_table)
    except Exception:  # noqa: BLE001
        pass
    retry_io(
        "create_faults_videos",
        lambda: conn.create_table(videos_table, data=ptr_table),
    )
    logger.info("videos_table %s rows=%d", videos_table, len(pointers))

    # 3) Chunk through the real pipeline, unchanged.
    chunk_openvid.run(
        cfg,
        source_table=videos_table,
        clips_table=clips_table,
        openvid_uri=base_uri,
        openvid_table="train",
        blob_column="video_blob",
        pointer_column="openvid_rowid",
        chunk_seconds=chunk_seconds,
        concurrency=concurrency,
        max_video_s=max_video_s,
        read_retries=read_retries,
        read_retry_sleep_s=read_retry_sleep_s,
    )

    # 4) Report: raw rows, full error detail, expected vs observed classes.
    clips = conn.open_table(clips_table)
    clips.checkout_latest()
    rows = (
        clips.search()
        .select(
            [
                "video_id",
                "chunk_id",
                "start_sec",
                "end_sec",
                "clip_bytes",
                "frame",
                "errors",
            ]
        )
        .limit(1000)
        .to_list()
    )
    rows.sort(
        key=lambda r: (
            r["video_id"],
            r["chunk_id"] is None,
            r["chunk_id"] if r["chunk_id"] is not None else -1,
        )
    )
    logger.info("clips_outcome (%d rows)\n%s", len(rows), format_sample(rows))
    for row in rows:
        for msg in row.get("errors") or []:
            logger.info(
                "error_detail video=%s chunk=%s %s",
                row["video_id"],
                row["chunk_id"],
                msg,
            )

    by_vid: dict[str, list[dict]] = {}
    for row in rows:
        by_vid.setdefault(row["video_id"], []).append(row)
    summary, failures = [], []
    for vid, expected in EXPECTED.items():
        vid_rows = by_vid.get(vid, [])
        observed = _error_classes(vid_rows)
        summary.append(
            {
                "video_id": vid,
                "expected": "/".join(sorted(expected)),
                "observed": "/".join(sorted(observed)) or "(no rows)",
                "clips": sum(1 for r in vid_rows if r.get("clip_bytes")),
            }
        )
        if expected == {"clean"}:
            if observed != {"clean"}:
                failures.append(
                    f"{vid}: expected clean rows only, observed {sorted(observed)}"
                )
        elif not (observed - {"clean"}):
            failures.append(
                f"{vid}: expected error rows, observed {sorted(observed) or 'no rows'}"
            )
        elif observed != expected:
            logger.warning(
                "class_mismatch video=%s expected=%s observed=%s "
                "(ffmpeg-version-sensitive; exact classes pinned in tests)",
                vid,
                sorted(expected),
                sorted(observed),
            )
    logger.info("expected_vs_observed\n%s", format_sample(summary))
    unexpected = sorted(set(by_vid) - set(EXPECTED))
    if unexpected:
        failures.append(f"unexpected video_ids in clips table: {unexpected}")

    # Persist the observed data-fault results (real; the demo injects only via
    # data) so a report builder can render them. Local path only.
    if not is_object_store:
        import geneva

        (Path(base_uri) / "report.json").write_text(
            json.dumps(
                {
                    "geneva_version": geneva.__version__,
                    "mode": cfg.mode,
                    "sources": len(pointers),
                    "data_faults": summary,
                    "ok": not failures,
                },
                indent=2,
            )
        )
        logger.info("wrote_report_data %s", Path(base_uri) / "report.json")

    if failures:
        for failure in failures:
            logger.error("faults_demo_check_failed %s", failure)
        raise SystemExit(1)
    logger.info("chunk_videos_faults_ok")
