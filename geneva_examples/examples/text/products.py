"""Synthetic product catalog — the source rows for the text workflow.

Generates a deterministic ``(product_id, title, description, category,
word_count)`` catalog from per-category vocabularies, so the example needs no
dataset download, no credentials, and no network: ``ingest-products`` writes
these rows straight into the table.

The descriptions are *composed per category* (materials, uses and audiences that
belong to that category) rather than drawn from one shared word bag. That makes
the catalog semantically separable, which is what gives the embedding stage
something real to show: a query like "warm layer for winter hiking" lands on the
apparel rows instead of returning noise.

Pure and import-cheap — no geneva, no torch. ``word_count`` is computed here, at
ingest time, so the materialized view's ``select`` can simply project it
alongside the UDF-computed embedding.
"""

from __future__ import annotations

import random
from typing import Any

import pyarrow as pa

# The catalog's Arrow schema. `word_count` is a plain int32 the view projects
# through; the embedding is added by the view's UDF, not stored here.
SCHEMA = pa.schema(
    [
        ("product_id", pa.string()),
        ("title", pa.string()),
        ("description", pa.string()),
        ("category", pa.string()),
        ("word_count", pa.int32()),
    ]
)


class _Vocab:
    """One category's word lists, assembled into titles and descriptions."""

    def __init__(
        self,
        *,
        materials: list[str],
        nouns: list[str],
        qualities: list[str],
        uses: list[str],
        audiences: list[str],
        material_clause: str = "made from {material}",
    ) -> None:
        self.materials = materials
        self.nouns = nouns
        self.qualities = qualities
        self.uses = uses
        self.audiences = audiences
        # How the material reads in a sentence — books have editions, not materials.
        self.material_clause = material_clause


# Five categories with disjoint vocabularies, so rows from different categories
# are far apart in embedding space and rows within one are close together.
VOCAB: dict[str, _Vocab] = {
    "apparel": _Vocab(
        materials=["merino wool", "brushed fleece", "organic cotton", "ripstop nylon"],
        nouns=["jacket", "base layer", "hoodie", "vest", "trail pants"],
        qualities=["breathable", "water-repellent", "insulated", "quick-drying"],
        uses=[
            "layering on cold-weather hikes",
            "wearing under a shell on winter climbs",
            "everyday wear that packs down small",
        ],
        audiences=["hikers", "trail runners", "commuters"],
    ),
    "kitchen": _Vocab(
        materials=["cast iron", "tri-ply stainless steel", "borosilicate glass"],
        nouns=["skillet", "stock pot", "chef's knife", "kettle", "mixing bowl"],
        qualities=["pre-seasoned", "dishwasher safe", "oven safe to 500F"],
        uses=[
            "searing steaks and finishing them in the oven",
            "simmering stocks and braises for hours",
            "everyday prep work on a small counter",
        ],
        audiences=["home cooks", "weeknight cooks", "batch-cooking households"],
    ),
    "electronics": _Vocab(
        materials=["anodized aluminum", "recycled polycarbonate", "tempered glass"],
        nouns=[
            "wireless earbuds",
            "portable monitor",
            "mechanical keyboard",
            "charger",
        ],
        qualities=[
            "USB-C powered",
            "low-latency",
            "with 30-hour battery life",
            "fanless",
        ],
        uses=[
            "working from a laptop in cafes and on trains",
            "calls and focus work in an open office",
            "a travel setup that fits in a sleeve",
        ],
        audiences=["remote workers", "students", "developers"],
    ),
    "garden": _Vocab(
        materials=["powder-coated steel", "cedar", "terracotta", "coco coir"],
        nouns=["raised bed", "pruning shears", "watering can", "planter", "trellis"],
        qualities=["rust-resistant", "frost-proof", "self-watering", "stackable"],
        uses=[
            "growing tomatoes and herbs on a patio",
            "starting seedlings before the last frost",
            "keeping a small balcony garden alive in summer heat",
        ],
        audiences=["balcony gardeners", "first-time growers", "allotment holders"],
    ),
    "books": _Vocab(
        materials=["paperback", "hardcover", "pocket-sized"],
        nouns=["field guide", "cookbook", "essay collection", "atlas", "novel"],
        qualities=["illustrated", "annotated", "with a foldout map", "revised"],
        uses=[
            "reading a chapter a night before bed",
            "identifying birds and plants on the trail",
            "gift-giving to someone starting a new hobby",
        ],
        audiences=["beginners", "gift buyers", "lifelong learners"],
        material_clause="in a {material} edition",
    ),
}

CATEGORIES: tuple[str, ...] = tuple(VOCAB)


def _titlecase(text: str) -> str:
    """Capitalize each word's first letter, leaving the rest alone.

    ``str.title()`` would produce ``Chef'S Knife`` and ``Tri-Ply`` — it uppercases
    after every non-letter.
    """
    return " ".join(word[:1].upper() + word[1:] for word in text.split())


def _row(rng: random.Random, index: int, category: str) -> dict[str, Any]:
    """Compose one catalog row from ``category``'s vocabulary."""
    v = VOCAB[category]
    material = rng.choice(v.materials)
    noun = rng.choice(v.nouns)
    quality = rng.choice(v.qualities)
    use = rng.choice(v.uses)
    audience = rng.choice(v.audiences)

    title = _titlecase(f"{material} {noun}")
    # Noun-first, so no row needs an article — "a"/"an" would have to be decided
    # per quality ("an insulated jacket" but "a USB-C powered charger").
    description = (
        f"{_titlecase(noun)}: {quality}, "
        f"{v.material_clause.format(material=material)}, built for {use}. "
        f"Designed with {audience} in mind."
    )
    return {
        "product_id": f"prod-{index:05d}",
        "title": title,
        "description": description,
        "category": category,
        # Counted here so the view projects a real column rather than deriving it.
        "word_count": len(description.split()),
    }


def generate_products(rows: int, *, seed: int = 42) -> list[dict[str, Any]]:
    """Build ``rows`` synthetic catalog rows, deterministic for a given ``seed``.

    Categories are assigned round-robin so every category is represented even
    for a small ``rows`` (the semantic search demo needs more than one cluster to
    be interesting), while the wording within each row is drawn from ``seed``.
    """
    if rows < 1:
        raise ValueError(f"rows must be >= 1, got {rows}")
    rng = random.Random(seed)  # noqa: S311 - sample text, not a security decision
    return [
        _row(rng, index, CATEGORIES[index % len(CATEGORIES)]) for index in range(rows)
    ]


def to_batches(
    products: list[dict[str, Any]], *, frag_size: int
) -> list[pa.RecordBatch]:
    """Slice ``products`` into ``SCHEMA``-typed record batches of ``frag_size``."""
    size = max(1, frag_size)
    return [
        pa.RecordBatch.from_pylist(products[i : i + size], schema=SCHEMA)
        for i in range(0, len(products), size)
    ]
