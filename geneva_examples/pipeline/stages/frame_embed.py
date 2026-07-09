"""Frame embedding CLI: OpenCLIP embeddings on video_clips.frame (GPU)."""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

import typer

from geneva_examples.core.common import connect, memory_request_bytes, setup_logging
from geneva_examples.core.config import load_config
from geneva_examples.pipeline.stages._runner import backfill_column

logger = logging.getLogger(__name__)

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def run(
    config: Path | None = typer.Option(None, "--config", help="Path to config.yaml."),
    log_level: str = typer.Option("INFO", help="Logging level."),
    db_uri: str | None = typer.Option(None, help="Override config db_uri."),
    table_name: str = typer.Option("video_clips", help="Table to operate on."),
    input_column: str = typer.Option("frame", help="Column of encoded images."),
    output_column: str = typer.Option("embedding", help="Output embedding column."),
    model_name: str = typer.Option("ViT-bigG-14", help="OpenCLIP architecture."),
    pretrained: str = typer.Option(
        "laion2b_s39b_b160k", help="OpenCLIP pretrained tag for --model-name."
    ),
    dim: int = typer.Option(
        1280,
        help="Embedding dimension; MUST match --model-name (ViT-bigG-14=1280, "
        "ViT-L-14=768, ViT-H-14=1024, ViT-B-32=512). The runner drops and "
        "re-adds the column at this width, so changing model + dim re-embeds.",
    ),
    batch_size: int = typer.Option(
        256, help="DataLoader batch size (256 keeps ViT-bigG VRAM comfortable on 80GB)."
    ),
    num_workers: int = typer.Option(
        12,
        help="DataLoader worker processes. With 2 tasks/GPU node, two actors share "
        "the 26 physical cores, so 2*12=24 fits.",
    ),
    num_cpus: float = typer.Option(
        4.0,
        help="CPUs reserved per model task. concurrency*num_cpus must stay within "
        "the cluster's total CPUs (72): 16*4=64 fits with two tasks per GPU.",
    ),
    num_gpus: float | None = typer.Option(
        None,
        help="GPUs per task (default 0.5 — two ViT-bigG tasks share each H100). "
        "Live monitoring showed bigG is feed-starved (GPUs sawtooth to 0% while "
        "CPU sits at ~4/26 cores), not GPU-bound, so packing 2 tasks/GPU lets one "
        "actor compute while the other stalls on JPEG decode; 2x model fits in 80GB.",
    ),
    memory_gib: int = typer.Option(1, help="Memory (GiB) per task (geneva caps <2)."),
    checkpoint_size: int = typer.Option(
        65536,
        help="Rows per UDF __call__; keep == task_size to amortize DataLoader churn.",
    ),
    task_size: int = typer.Option(65536, help="Rows per read task."),
    concurrency: int = typer.Option(
        16, help="Backfill concurrency (two tasks per GPU)."
    ),
    backfill_timeout_min: int = typer.Option(1000, help="Per-backfill timeout (min)."),
    flush_interval_s: float = typer.Option(30.0, help="Checkpoint flush interval (s)."),
    schema_wait_attempts: int = typer.Option(30, help="Schema-visibility attempts."),
    schema_wait_sleep_s: int = typer.Option(2, help="Seconds between schema checks."),
) -> None:
    """Add an OpenCLIP embedding column to the frames table."""
    setup_logging(log_level)
    os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")

    import geneva
    from geneva.manifest import GenevaManifest

    from geneva_examples.udfs.clip import CLIP_RUNTIME_PIP, build_clip_embedding_udf

    cfg = load_config(config)
    if db_uri:
        cfg.db_uri = db_uri
    resolved_gpus = num_gpus if num_gpus is not None else 0.5

    logger.info("geneva_version %s", geneva.__version__)
    logger.info("db_uri %s table %s column %s", cfg.db_uri, table_name, input_column)
    logger.info(
        "model %s pretrained %s dim %s batch_size %s num_workers %s num_gpus %s",
        model_name,
        pretrained,
        dim,
        batch_size,
        num_workers,
        resolved_gpus,
    )

    conn = connect(cfg)
    table = conn.open_table(table_name)

    manifest = (
        GenevaManifest.create_pip(f"frame-embed-{uuid.uuid4().hex[:6]}")
        .pip(CLIP_RUNTIME_PIP)
        .build()
    )
    udf = build_clip_embedding_udf(
        input_column=input_column,
        manifest=manifest,
        batch_size=batch_size,
        num_workers=num_workers,
        num_cpus=num_cpus,
        num_gpus=resolved_gpus,
        memory_bytes=memory_request_bytes(memory_gib),
        checkpoint_size=checkpoint_size,
        task_size=task_size,
        model_name=model_name,
        pretrained=pretrained,
        dim=dim,
    )
    backfill_column(
        conn=conn,
        table=table,
        table_name=table_name,
        column=output_column,
        udf=udf,
        concurrency=concurrency,
        task_size=task_size,
        checkpoint_size=checkpoint_size,
        flush_interval_s=flush_interval_s,
        timeout_min=backfill_timeout_min,
        wait_attempts=schema_wait_attempts,
        wait_sleep_s=schema_wait_sleep_s,
    )
    logger.info("frame_embed_ok")


if __name__ == "__main__":
    app()
