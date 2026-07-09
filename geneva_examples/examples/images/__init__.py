"""Image feature pipeline — a self-contained example.

Loads images from a Hugging Face dataset, then backfills feature columns with
Geneva UDFs: cheap CPU metadata, OpenCLIP embeddings, and BLIP captions.
"""

from __future__ import annotations

from geneva_examples.core.spec import Example, Param, Step
from geneva_examples.examples.images import caption, embed, ingest, lightweight

_TABLE = Param("table_name", str, "images", "Target table name.")
_TIMEOUT = Param(
    "backfill_timeout_min", int, 1000, "Per-backfill timeout (min).", min=1
)
_FLUSH = Param(
    "flush_interval_s", float, 30.0, "Checkpoint flush interval (seconds).", min=0
)
_WAIT_ATTEMPTS = Param(
    "schema_wait_attempts", int, 30, "Schema-visibility attempts.", min=1
)
_WAIT_SLEEP = Param(
    "schema_wait_sleep_s", int, 2, "Seconds between schema checks.", min=0
)


def _model_params() -> tuple[Param, ...]:
    return (
        Param(
            "batch_size",
            int,
            1024,
            "DataLoader batch size (auto-shrunk locally).",
            min=1,
        ),
        Param("num_workers", int, 8, "DataLoader worker processes (0 locally).", min=0),
        Param("num_cpus", float, 8.0, "CPUs per model task (capped locally).", min=0.0),
        Param("num_gpus", float, None, "GPUs per task (forced to 0 locally)."),
        Param("memory_gib", int, 1, "Memory (GiB) per task (geneva caps <2).", min=1),
        Param("checkpoint_size", int, 1024, "Rows per UDF __call__.", min=1),
        Param("task_size", int, 1024, "Rows per read task.", min=1),
        Param("concurrency", int, 8, "Backfill concurrency (1 locally).", min=1),
        _TIMEOUT,
        _FLUSH,
        _WAIT_ATTEMPTS,
        _WAIT_SLEEP,
    )


INGEST = Step(
    key="ingest-images",
    title="Ingest images",
    description=(
        "Create the table and stream images from a Hugging Face dataset into an "
        "`image` (bytes) + `image_id`/`label` schema, ready for the feature steps."
    ),
    run=ingest.run,
    params=(
        _TABLE,
        Param("num_images", int, 75, "Number of images to load.", min=1),
        Param("frag_size", int, 25, "Images per record batch.", min=1),
        Param("hf_dataset", str, "timm/oxford-iiit-pet", "Hugging Face dataset name."),
        Param("hf_split", str, "train", "Hugging Face dataset split."),
        Param("overwrite", bool, True, "Drop the table first if it exists."),
        Param("table_write_retries", int, 5, "Retries for create/add ops.", min=0),
        Param(
            "table_write_retry_sleep_s",
            float,
            2.0,
            "Base sleep (s) between table-write retries.",
            min=0,
        ),
    ),
)

LIGHTWEIGHT = Step(
    key="lightweight",
    title="File size + dimensions",
    description=(
        "Backfill two cheap CPU columns — `file_size` (byte length) and "
        "`dimensions` (width/height) — a fast smoke test of the backfill path."
    ),
    run=lightweight.run,
    requires="run ingest-images first",
    params=(
        _TABLE,
        _TIMEOUT,
        Param("backfill_concurrency", int, 32, "Backfill concurrency.", min=1),
        Param("backfill_task_size", int, 256, "Backfill task size.", min=1),
        Param("backfill_checkpoint_size", int, 128, "Backfill checkpoint size.", min=1),
        Param(
            "backfill_flush_interval_s",
            float,
            30.0,
            "Batch checkpoint flush interval (seconds).",
            min=0,
        ),
        Param("use_cpu_only_pool", bool, True, "Use the CPU-only pool (enterprise)."),
        _WAIT_ATTEMPTS,
        _WAIT_SLEEP,
    ),
)

EMBED = Step(
    key="embed",
    title="OpenCLIP embeddings",
    description=(
        "Backfill an `embedding` column with OpenCLIP ViT-B-32, then (optionally) "
        "run a local text→image search demo. Runs on GPU in enterprise mode, CPU "
        "locally."
    ),
    run=embed.run,
    gpu=True,
    requires="run ingest-images first",
    params=(
        _TABLE,
        Param(
            "query_text", str, "a golden retriever", "Text query for the search demo."
        ),
        *_model_params(),
        Param("search_demo", bool, True, "Run the local text→image search demo after."),
    ),
)

CAPTION = Step(
    key="caption",
    title="BLIP captions",
    description=(
        "Backfill a BLIP caption column (`caption_blip`). "
        "GPU in enterprise mode, CPU locally."
    ),
    run=caption.run,
    gpu=True,
    requires="run ingest-images first",
    params=(
        _TABLE,
        *_model_params(),
        Param(
            "caption_local_preview",
            bool,
            False,
            "Log a local BLIP caption for one image before backfill.",
        ),
    ),
)

EXAMPLE = Example(
    name="images",
    title="Image feature pipeline",
    description=(
        "Load images from Hugging Face, then enrich them with Geneva UDF "
        "backfills: cheap CPU metadata, OpenCLIP embeddings, and BLIP captions.\n\n"
        "Start with **Ingest images**, then run any feature step."
    ),
    modality="image",
    steps=(INGEST, LIGHTWEIGHT, EMBED, CAPTION),
)
