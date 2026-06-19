"""Hugging Face image loading and table image decoding."""

import logging
from io import BytesIO

import pyarrow as pa

logger = logging.getLogger(__name__)


def load_hf_image_batches(
    dataset_name: str, split: str, num_images: int, frag_size: int
) -> list[pa.RecordBatch]:
    """Load images from a Hugging Face dataset into PyArrow record batches."""
    from datasets import load_dataset

    batches: list[pa.RecordBatch] = []
    batch = []
    dataset = load_dataset(dataset_name, split=f"{split}[:{num_images}]")
    for idx, row in enumerate(dataset):
        image = row["image"]
        buf = BytesIO()
        image.save(buf, format="PNG")
        out = {
            "image": buf.getvalue(),
            "label": row.get("label"),
            "image_id": row.get("image_id", idx),
            "label_cat_dog": row.get("label_cat_dog"),
        }
        batch.append(out)
        if len(batch) >= frag_size:
            batches.append(pa.RecordBatch.from_pylist(batch))
            batch = []
    if batch:
        batches.append(pa.RecordBatch.from_pylist(batch))
    return batches


def decode_images_from_table(table: object, limit: int | None = None) -> list[object]:
    """Decode stored PNG bytes from ``table`` into PIL images."""
    from PIL import Image

    query = table.search().select(["image_id", "image"])
    if limit is not None:
        query = query.limit(limit)
    rows = query.to_list()
    images = []
    sizes = []
    for row in rows:
        with Image.open(BytesIO(row["image"])) as img:
            sizes.append(
                {"image_id": row["image_id"], "size": img.size, "mode": img.mode}
            )
            images.append(img.copy())
    logger.info("decoded_preview %s", sizes)
    return images
