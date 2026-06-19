"""Embedding stage: OpenCLIP ViT-B-32 image embeddings (GPU)."""

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
    table_name: str | None = typer.Option(None, help="Override config table_name."),
    query_text: str = typer.Option(
        "a golden retriever", help="Text query for the embedding search demo."
    ),
    batch_size: int = typer.Option(1024, help="DataLoader batch size."),
    num_workers: int = typer.Option(8, help="DataLoader worker processes."),
    num_cpus: float = typer.Option(8.0, help="CPUs per model task."),
    num_gpus: float | None = typer.Option(None, help="GPUs per task (default 1.0)."),
    memory_gib: int = typer.Option(1, help="Memory (GiB) per task (geneva caps <2)."),
    checkpoint_size: int = typer.Option(1024, help="Rows per UDF __call__."),
    task_size: int = typer.Option(1024, help="Rows per read task."),
    concurrency: int = typer.Option(8, help="Backfill concurrency."),
    backfill_timeout_min: int = typer.Option(1000, help="Per-backfill timeout (min)."),
    flush_interval_s: float = typer.Option(30.0, help="Checkpoint flush interval (s)."),
    schema_wait_attempts: int = typer.Option(30, help="Schema-visibility attempts."),
    schema_wait_sleep_s: int = typer.Option(2, help="Seconds between schema checks."),
) -> None:
    """Add an OpenCLIP `embedding` column to the configured table."""
    setup_logging(log_level)
    os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")

    import geneva
    from geneva.manifest import GenevaManifest

    from geneva_examples.udfs.clip import CLIP_RUNTIME_PIP, build_clip_embedding_udf

    cfg = load_config(config)
    if db_uri:
        cfg.db_uri = db_uri
    if table_name:
        cfg.table_name = table_name
    resolved_gpus = num_gpus if num_gpus is not None else 1.0

    logger.info("geneva_version %s", geneva.__version__)
    logger.info("db_uri %s table %s", cfg.db_uri, cfg.table_name)

    conn = connect(cfg)
    table = conn.open_table(cfg.table_name)

    manifest = (
        GenevaManifest.create_pip(f"embed-{uuid.uuid4().hex[:6]}")
        .pip(CLIP_RUNTIME_PIP)
        .build()
    )
    udf = build_clip_embedding_udf(
        input_column="image",
        manifest=manifest,
        batch_size=batch_size,
        num_workers=num_workers,
        num_cpus=num_cpus,
        num_gpus=resolved_gpus,
        memory_bytes=memory_request_bytes(memory_gib),
        checkpoint_size=checkpoint_size,
        task_size=task_size,
    )
    table = backfill_column(
        conn=conn,
        table=table,
        table_name=cfg.table_name,
        column="embedding",
        udf=udf,
        concurrency=concurrency,
        task_size=task_size,
        checkpoint_size=checkpoint_size,
        flush_interval_s=flush_interval_s,
        timeout_min=backfill_timeout_min,
        wait_attempts=schema_wait_attempts,
        wait_sleep_s=schema_wait_sleep_s,
    )

    import open_clip
    import torch

    model, _, _ = open_clip.create_model_and_transforms(
        "ViT-B-32", pretrained="laion2b_s34b_b79k"
    )
    tokenizer = open_clip.get_tokenizer("ViT-B-32")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = model.to(device).eval()
    tokens = tokenizer([query_text]).to(device)
    with torch.no_grad():
        query_emb = model.encode_text(tokens)
        query_emb /= query_emb.norm(dim=-1, keepdim=True)
    query_vec = query_emb.squeeze().cpu().numpy().astype(float).tolist()
    rows = table.search(query_vec, "embedding").limit(5).to_list()
    logger.info("embedding_query %s matches %s", query_text, len(rows))
    logger.info(
        "embedding_sample %s",
        [{"image_id": r.get("image_id"), "label": r.get("label")} for r in rows[:5]],
    )
    logger.info("embeddings_ok")


if __name__ == "__main__":
    app()
