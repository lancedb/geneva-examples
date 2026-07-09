"""Image ingest step: load Hugging Face images into the configured table."""

from __future__ import annotations

import logging
import os

from geneva_examples.core.common import connect, format_sample
from geneva_examples.core.config import Config
from geneva_examples.core.utils.retry import retry_io

logger = logging.getLogger(__name__)


def run(
    cfg: Config,
    *,
    table_name: str = "images",
    num_images: int = 75,
    frag_size: int = 25,
    hf_dataset: str = "timm/oxford-iiit-pet",
    hf_split: str = "train",
    overwrite: bool = True,
    table_write_retries: int = 5,
    table_write_retry_sleep_s: float = 2.0,
) -> None:
    """Create the table and load images from a Hugging Face dataset."""
    os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")
    os.environ.setdefault("HF_HOME", "./huggingface_cache")

    import geneva

    from geneva_examples.core.utils.images import load_hf_image_batches

    logger.info("geneva_version %s mode %s", geneva.__version__, cfg.mode)
    logger.info("db_uri %s table %s", cfg.db_uri, table_name)
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
            conn.drop_table(table_name)
            logger.info("dropped_existing_table %s", table_name)
        except Exception:  # noqa: BLE001
            pass

    table = retry_io(
        "create_table",
        lambda: conn.create_table(table_name, data=image_batches[0]),
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
        "initial_sample\n%s",
        format_sample(table.search().select(["image_id", "label"]).limit(5).to_list()),
    )
    logger.info("ingest_images_ok")
