"""Download videos into PyArrow record batches for ingest."""

from __future__ import annotations

import logging
import urllib.request
from pathlib import Path

import pyarrow as pa

logger = logging.getLogger(__name__)

# Some CDNs (e.g. Blender's) reject the default Python-urllib User-Agent with 403.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# Socket timeout (seconds) for each blocking connect/read, so a stalled CDN
# fails instead of hanging the ingest forever.
_DOWNLOAD_TIMEOUT_S = 60


def _download(url: str, dest: Path) -> bytes:
    """Download ``url`` to ``dest`` (skipping if cached) and return its bytes."""
    if dest.exists() and dest.stat().st_size > 0:
        logger.info("cache_hit %s (%d bytes)", dest.name, dest.stat().st_size)
        return dest.read_bytes()

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    logger.info("downloading %s -> %s", url, dest)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with (
            urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT_S) as resp,
            open(tmp, "wb") as out,
        ):
            while chunk := resp.read(1 << 20):
                out.write(chunk)
        tmp.replace(dest)
    except BaseException:
        # Don't leave a truncated .part behind on error/timeout/Ctrl-C.
        tmp.unlink(missing_ok=True)
        raise
    logger.info("downloaded %s (%d bytes)", dest.name, dest.stat().st_size)
    return dest.read_bytes()


def download_video_batches(
    videos: list[tuple[str, str]],
    cache_dir: str,
    frag_size: int = 1,
) -> list[pa.RecordBatch]:
    """Download ``(video_id, url)`` pairs into ``video_id`` + ``video`` batches.

    Videos are cached under ``cache_dir`` (keyed by ``video_id``). ``frag_size``
    rows per batch — the default of 1 puts each video in its own fragment so the
    remote chunker can process them in parallel.
    """
    cache = Path(cache_dir)
    batches: list[pa.RecordBatch] = []
    batch: list[dict] = []
    for video_id, url in videos:
        suffix = Path(url).suffix or ".mp4"
        data = _download(url, cache / f"{video_id}{suffix}")
        batch.append({"video_id": video_id, "video": data})
        if len(batch) >= frag_size:
            batches.append(_to_batch(batch))
            batch = []
    if batch:
        batches.append(_to_batch(batch))
    return batches


def _to_batch(rows: list[dict]) -> pa.RecordBatch:
    schema = pa.schema(
        [
            pa.field("video_id", pa.string()),
            pa.field("video", pa.large_binary()),
        ]
    )
    return pa.RecordBatch.from_pylist(rows, schema=schema)


# --- OpenVid (lance-format/openvid-lance) ingest helpers -------------------

# Columns pulled from the OpenVid lance dataset, in scan order. `video_path`
# becomes our `video_id`; `video_blob` is scanned only as a descriptor (no bytes)
# to drop null-video rows — the chunker reads the blob later via the captured
# `_rowid`. The rest ride along as metadata. See
# https://huggingface.co/datasets/lance-format/openvid-lance.
OPENVID_SOURCE_COLUMNS = [
    "video_path",
    "video_blob",
    "caption",
    "embedding",
    "aesthetic_score",
    "motion_score",
    "temporal_consistency_score",
    "camera_motion",
    "fps",
    "seconds",
    "frame",
]

# Embedding dimensionality of the OpenVid `embedding` column.
_OPENVID_EMBEDDING_DIM = 1024


def _openvid_target_schema() -> pa.Schema:
    """Destination ``videos``-table schema for normalized OpenVid rows.

    Reference-only: the raw MP4 bytes are *not* ingested. ``video_id`` matches
    the existing ``videos`` schema, ``openvid_rowid`` is a stable pointer back to
    the source row (used by the chunker's ``take_blobs(ids=...)`` to read the
    blob on the cluster), and the rest are metadata carried through from OpenVid,
    joinable on ``video_id``.
    """
    return pa.schema(
        [
            pa.field("video_id", pa.string()),  # <- video_path
            pa.field("openvid_rowid", pa.int64()),  # <- source _rowid pointer
            pa.field("caption", pa.string()),
            pa.field("embedding", pa.list_(pa.float32(), _OPENVID_EMBEDDING_DIM)),
            pa.field("aesthetic_score", pa.float64()),
            pa.field("motion_score", pa.float64()),
            pa.field("temporal_consistency_score", pa.float64()),
            pa.field("camera_motion", pa.string()),
            pa.field("fps", pa.float64()),
            pa.field("seconds", pa.float64()),
            pa.field("frame", pa.int64()),
        ]
    )


def normalize_openvid_reference_batch(
    batch: pa.RecordBatch, *, skip_null_video: bool = True
) -> pa.RecordBatch:
    """Map an OpenVid scan batch to the reference-only ``videos``-table schema.

    The batch must come from a scan with ``with_row_id=True`` and the *default*
    blob handling, so ``video_blob`` is a descriptor (no bytes materialized) and
    ``_rowid`` is present. Maps ``video_path -> video_id`` and the captured
    ``_rowid -> openvid_rowid`` pointer, carries metadata through, and (when
    ``skip_null_video``) drops rows whose blob descriptor is null since they
    can't be chunked downstream. Returns a batch matching
    ``_openvid_target_schema()`` (empty batch if all rows were filtered).
    """
    import pyarrow.compute as pc

    schema = _openvid_target_schema()
    tbl = pa.Table.from_batches([batch], schema=batch.schema)

    if skip_null_video:
        # Filter on the blob *descriptor* (no bytes pulled): a null/absent blob
        # has no source video to chunk.
        tbl = tbl.filter(pc.is_valid(tbl["video_blob"]))  # ty: ignore[unresolved-attribute]  # third-party stub gap
    if tbl.num_rows == 0:
        return pa.RecordBatch.from_pylist([], schema=schema)

    embedding_col = tbl["embedding"]
    target_embedding = schema.field("embedding").type
    # Source is usually fixed_size_list<float32,1024>; cast defensively in case a
    # scan surfaces it as variable-length list<float32> or list<float64>.
    if not embedding_col.type.equals(target_embedding):
        embedding_col = embedding_col.cast(target_embedding)

    out = pa.table(
        {
            "video_id": tbl["video_path"].cast(pa.string()),
            "openvid_rowid": tbl["_rowid"].cast(pa.int64()),
            "caption": tbl["caption"],
            "embedding": embedding_col,
            "aesthetic_score": tbl["aesthetic_score"],
            "motion_score": tbl["motion_score"],
            "temporal_consistency_score": tbl["temporal_consistency_score"],
            "camera_motion": tbl["camera_motion"],
            "fps": tbl["fps"],
            "seconds": tbl["seconds"],
            "frame": tbl["frame"],
        },
        schema=schema,
    )
    return out.combine_chunks().to_batches()[0]
