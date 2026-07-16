"""Video clip pipeline — a self-contained example.

Ingest videos (or reference an OpenVid dataset), chunk them into fixed-length
clips + a start frame, then backfill per-frame features (OpenCLIP embeddings,
BLIP captions, OpenPose skeletons).
"""

from __future__ import annotations

from geneva_examples.core.spec import (
    COMMON_HELP,
    Example,
    Step,
    params_from_signature,
)
from geneva_examples.examples.video import (
    chunk,
    chunk_external_video,
    chunk_openvid,
    frame_caption,
    frame_embed,
    frame_openpose,
    ingest,
    ingest_external_refs,
    ingest_openvid,
    seed,
)

INGEST = Step(
    key="ingest-videos",
    title="Ingest videos",
    description=(
        "Download a couple of Creative-Commons MP4s into a `videos` table "
        "(`video_id` + raw `video` bytes), ready for chunking."
    ),
    run=ingest.run,
    params=params_from_signature(
        ingest.run,
        help=COMMON_HELP | {"cache_dir": "Directory to cache downloaded videos."},
    ),
)

INGEST_OPENVID = Step(
    key="ingest-videos-openvid",
    title="Ingest videos (OpenVid, reference-only)",
    description=(
        "Register the first N rows of the OpenVid Lance dataset as a "
        "reference-only `videos` table (metadata + a blob pointer, no bytes). The "
        "chunker reads each clip's blob directly on the worker."
    ),
    run=ingest_openvid.run,
    params=params_from_signature(
        ingest_openvid.run,
        help=COMMON_HELP
        | {
            "limit": "Max OpenVid rows to ingest (first N in scan order).",
            "openvid_uri": "Base URI holding the OpenVid lance dataset.",
            "openvid_table": "OpenVid dataset name (<uri>/<table>.lance).",
            "skip_null_video": "Drop rows whose video bytes are null.",
        },
    ),
)

INGEST_EXTERNAL = Step(
    key="ingest-videos-external",
    title="Ingest videos (external refs, reference-only)",
    description=(
        "Enumerate an S3-compatible video bucket and write a reference-only "
        "`videos` table (video_id + video_uri + size_mb, no bytes). The "
        "chunk-videos-external step reads each URI on the worker."
    ),
    run=ingest_external_refs.run,
    params=params_from_signature(
        ingest_external_refs.run,
        help=COMMON_HELP
        | {
            "video_bucket": "Video bucket name (or VIDEO_S3_BUCKET).",
            "video_endpoint": "S3 endpoint URL — required, e.g. a non-AWS S3 service (or VIDEO_S3_ENDPOINT).",
            "video_access_key": "Video-bucket access key (or VIDEO_S3_ACCESS_KEY).",
            "video_secret_key": "Video-bucket secret key (or VIDEO_S3_SECRET_KEY).",
            "video_region": "SigV4 region (or VIDEO_S3_REGION).",
            "prefix": "Only list keys under this prefix.",
            "suffix": "Only keep keys with this suffix.",
            "limit": "Max videos to register (0 = all).",
            "smallest_first": "Register the smallest N first (handy for spikes).",
            "sample": (
                "Selection mode: '' (smallest_first/listing order) or 'stride' "
                "— a systematic sample across the size distribution (representative)."
            ),
        },
    ),
)

CHUNK = Step(
    key="chunk-videos",
    title="Chunk videos into clips",
    description=(
        "Split each video into `chunk-seconds` clips via a Geneva chunker UDTF, "
        "materializing a `video_clips` table with `clip_bytes` + a start `frame`."
    ),
    run=chunk.run,
    requires="run ingest-videos first",
    params=params_from_signature(
        chunk.run,
        help=COMMON_HELP
        | {
            "source_task_size": "Source rows per chunker expansion task.",
            "max_clips": "Cap clips per video (default: all).",
            "max_video_s": "Skip videos longer than this many seconds.",
        },
    ),
)

CHUNK_OPENVID = Step(
    key="chunk-videos-openvid",
    title="Chunk videos (OpenVid blobs)",
    description=(
        "Chunk the reference-only OpenVid `videos` table into 1-second clips, "
        "reading each blob from the source dataset on the worker via `take_blobs`."
    ),
    run=chunk_openvid.run,
    requires="run ingest-videos-openvid first",
    params=params_from_signature(
        chunk_openvid.run,
        help=COMMON_HELP
        | {
            "blob_column": "Blob column in the source dataset.",
            "pointer_column": "Source-row pointer column in the videos table.",
            "read_retries": "Per-row blob read attempts.",
            "read_retry_sleep_s": "Base sleep (s) for blob-read backoff.",
            "source_task_size": "Source rows per chunker expansion task.",
            "max_clips": "Cap clips per video (default: all).",
            "max_video_s": "Skip videos longer than this many seconds.",
        },
    ),
)

CHUNK_EXTERNAL = Step(
    key="chunk-videos-external",
    title="Chunk videos (external refs)",
    description=(
        "Chunk the reference-only URI `videos` table into clips, reading each "
        "video from its S3 URI on the worker with the video token."
    ),
    run=chunk_external_video.run,
    requires="run ingest-videos-external first",
    params=params_from_signature(
        chunk_external_video.run,
        help=COMMON_HELP
        | {
            "uri_column": "URI pointer column in the videos table.",
            "video_endpoint": "S3 endpoint URL — required, e.g. a non-AWS S3 service (or VIDEO_S3_ENDPOINT).",
            "video_access_key": "Video-bucket access key (or VIDEO_S3_ACCESS_KEY).",
            "video_secret_key": "Video-bucket secret key (or VIDEO_S3_SECRET_KEY).",
            "video_region": "SigV4 region (or VIDEO_S3_REGION).",
            "source_task_size": "Source rows per chunker task (1 = fan out per video).",
            "max_clips": "Cap clips per video (default: all).",
            "max_video_s": "Skip videos longer than this many seconds.",
            "max_video_mb": "Skip videos larger than this many MB (guards actor RAM).",
            "read_retries": "Per-video read attempts.",
            "read_retry_sleep_s": "Base sleep (s) for read backoff.",
            "detach": "Submit and return a job id without waiting (enterprise only).",
        },
    ),
)

FRAME_EMBED = Step(
    key="frame-embed",
    title="OpenCLIP embeddings on frames",
    description="Backfill an OpenCLIP embedding on each clip's start frame.",
    run=frame_embed.run,
    gpu=True,
    requires="run a chunk step first",
    params=params_from_signature(
        frame_embed.run,
        help=COMMON_HELP
        | {
            "model_name": "OpenCLIP architecture.",
            "pretrained": "OpenCLIP pretrained tag for --model-name.",
            "dim": "Embedding dimension; MUST match --model-name.",
            "reset": (
                "Drop and recompute the whole embedding column (destructive). "
                "Default off = incremental: only embed clips still missing it, "
                "so it is safe to run alongside a chunk job and to re-run."
            ),
        },
    ),
)

FRAME_CAPTION = Step(
    key="frame-caption",
    title="BLIP captions on frames",
    description="Backfill a BLIP caption on each clip's start frame.",
    run=frame_caption.run,
    gpu=True,
    requires="run a chunk step first",
    params=params_from_signature(frame_caption.run, help=COMMON_HELP),
)

FRAME_OPENPOSE = Step(
    key="frame-openpose",
    title="OpenPose skeletons on frames",
    description="Backfill an OpenPose pose-skeleton PNG for each clip's frame.",
    run=frame_openpose.run,
    gpu=True,
    requires="run a chunk step first",
    params=params_from_signature(
        frame_openpose.run,
        help=COMMON_HELP
        | {
            "include_hand": "Include hand keypoints.",
            "include_face": "Include face keypoints.",
        },
    ),
)

SEED = Step(
    key="seed-video-clips",
    title="Seed clips (load-test helper)",
    description=(
        "Replicate one decoded clip into N identical `video_clips` rows — a fast "
        "way to load-test the frame stages without a full chunk run."
    ),
    run=seed.run,
    params=params_from_signature(
        seed.run,
        help=COMMON_HELP
        | {
            "num_rows": "Number of identical rows to write.",
            "include_clip_bytes": "Also replicate the clip_bytes column.",
            "seed_clip_table": "Reuse a clip from this existing clips table.",
            "source_video_id": "Pick this source video_id as the basis.",
        },
    ),
)

EXAMPLE = Example(
    name="video",
    title="Video clip pipeline",
    description=(
        "Ingest videos, chunk them into fixed-length clips with a start frame, "
        "then backfill per-frame features. Two ingest/chunk variants: local MP4 "
        "downloads, or reference-only OpenVid blobs.\n\n"
        "Order: **ingest → chunk → frame-\\***."
    ),
    modality="video",
    steps=(
        INGEST,
        INGEST_OPENVID,
        INGEST_EXTERNAL,
        CHUNK,
        CHUNK_OPENVID,
        CHUNK_EXTERNAL,
        FRAME_EMBED,
        FRAME_CAPTION,
        FRAME_OPENPOSE,
        SEED,
    ),
)
