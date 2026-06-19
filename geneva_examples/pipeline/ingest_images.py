"""Image ingest CLI: load Hugging Face images into the configured LanceDB table."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import typer

from geneva_examples.core.common import connect, setup_logging
from geneva_examples.core.config import load_config
from geneva_examples.core.utils.retry import retry_io

logger = logging.getLogger(__name__)

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def run(
    config: Path | None = typer.Option(None, "--config", help="Path to config.yaml."),
    log_level: str = typer.Option("INFO", help="Logging level."),
    db_uri: str | None = typer.Option(None, help="Override config db_uri."),
    table_name: str | None = typer.Option(None, help="Override config table_name."),
    num_images: int = typer.Option(75, help="Number of images to load."),
    frag_size: int = typer.Option(25, help="Images per record batch."),
    hf_dataset: str = typer.Option(
        "timm/oxford-iiit-pet", help="Hugging Face dataset name."
    ),
    hf_split: str = typer.Option("train", help="Hugging Face dataset split."),
    overwrite: bool = typer.Option(
        True, help="Drop the table first if it already exists."
    ),
    table_write_retries: int = typer.Option(5, help="Retries for create/add ops."),
    table_write_retry_sleep_s: float = typer.Option(
        2.0, help="Base sleep (seconds) between table-write retries."
    ),
) -> None:
    """Create the table and load images from a Hugging Face dataset."""
    setup_logging(log_level)
    os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")
    os.environ.setdefault("HF_HOME", "./huggingface_cache")

    import geneva

    from geneva_examples.core.utils.images import load_hf_image_batches

    cfg = load_config(config)
    if db_uri:
        cfg.db_uri = db_uri
    if table_name:
        cfg.table_name = table_name

    logger.info("geneva_version %s", geneva.__version__)
    logger.info("db_uri %s table %s", cfg.db_uri, cfg.table_name)
    logger.info(
        "hf_dataset %s hf_split %s rows_target %s", hf_dataset, hf_split, num_images
    )

    conn = connect(cfg)

    image_batches = load_hf_image_batches(
        dataset_name=hf_dataset,
        split=hf_split,
        num_images=num_images,
        frag_size=frag_size,
    )
    if not image_batches:
        raise RuntimeError("no images loaded from Hugging Face dataset")

    if overwrite:
        try:
            conn.drop_table(cfg.table_name)
            logger.info("dropped_existing_table %s", cfg.table_name)
        except Exception:  # noqa: BLE001
            pass

    table = retry_io(
        "create_table",
        lambda: conn.create_table(cfg.table_name, data=image_batches[0]),
        attempts=table_write_retries,
        sleep_s=table_write_retry_sleep_s,
    )
    total_batches = len(image_batches)
    for batch_index, batch in enumerate(image_batches[1:], start=2):
        retry_io(
            f"add_batch_{batch_index}",
            lambda batch=batch: table.add(batch),
            attempts=table_write_retries,
            sleep_s=table_write_retry_sleep_s,
        )
        if batch_index % 50 == 0 or batch_index == total_batches:
            logger.info("batches_loaded %d of %d", batch_index, total_batches)

    logger.info("rows_created %s", table.count_rows())
    try:
        logger.info("table_names %s", conn.table_names())
    except Exception:  # noqa: BLE001
        pass
    logger.info(
        "initial_sample %s",
        table.search().select(["image_id", "label"]).limit(5).to_list(),
    )
    logger.info("ingest_images_ok")


if __name__ == "__main__":
    app()
