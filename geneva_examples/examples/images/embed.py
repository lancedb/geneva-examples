"""Embedding step: OpenCLIP ViT-B-32 image embeddings (GPU; CPU in local mode)."""

from __future__ import annotations

import logging
import os

from geneva_examples.core.backfill import backfill_column
from geneva_examples.core.common import (
    build_manifest,
    connect,
    format_sample,
    local_or,
    resolve_resources,
    runtime_session,
)
from geneva_examples.core.config import Config

logger = logging.getLogger(__name__)


def run(
    cfg: Config,
    *,
    table_name: str = "images",
    query_text: str = "a golden retriever",
    batch_size: int = 1024,
    num_workers: int = 8,
    num_cpus: float = 8.0,
    num_gpus: float | None = None,
    memory_gib: int = 1,
    checkpoint_size: int = 1024,
    task_size: int = 1024,
    concurrency: int = 8,
    backfill_timeout_min: int = 1000,
    flush_interval_s: float = 30.0,
    schema_wait_attempts: int = 30,
    schema_wait_sleep_s: int = 2,
    search_demo: bool = True,
) -> None:
    """Add an OpenCLIP `embedding` column to the configured table."""
    os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")

    import geneva

    from geneva_examples.examples._shared.clip import (
        CLIP_RUNTIME_PIP,
        build_clip_embedding_udf,
    )

    resolved_gpus = num_gpus if num_gpus is not None else 1.0
    # Local mode: no GPU, few cores — clamp resources and shrink the CPU-bound
    # DataLoader/backfill knobs so the run finishes on a laptop.
    num_cpus, resolved_gpus, memory_bytes = resolve_resources(
        cfg, num_cpus=num_cpus, num_gpus=resolved_gpus, memory_gib=memory_gib
    )
    batch_size = local_or(cfg, 8, batch_size)
    num_workers = local_or(cfg, 0, num_workers)
    concurrency = local_or(cfg, 1, concurrency)

    logger.info("geneva_version %s mode %s", geneva.__version__, cfg.mode)
    logger.info("db_uri %s table %s", cfg.db_uri, table_name)

    conn = connect(cfg)
    table = conn.open_table(table_name)

    manifest = build_manifest(cfg, "embed", CLIP_RUNTIME_PIP)
    udf = build_clip_embedding_udf(
        input_column="image",
        manifest=manifest,
        batch_size=batch_size,
        num_workers=num_workers,
        num_cpus=num_cpus,
        num_gpus=resolved_gpus,
        memory_bytes=memory_bytes,
        checkpoint_size=checkpoint_size,
        task_size=task_size,
    )
    with runtime_session(conn, cfg):
        table = backfill_column(
            conn=conn,
            table=table,
            table_name=table_name,
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

    # Local text->image search demo. Gated behind --search-demo because it
    # imports open_clip + torch and downloads model weights on the driver — the
    # backfill itself runs those remotely, so the stage works without them.
    if search_demo:
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
            "embedding_sample\n%s",
            format_sample(
                [
                    {"image_id": r.get("image_id"), "label": r.get("label")}
                    for r in rows[:5]
                ]
            ),
        )

    logger.info("embeddings_ok")
