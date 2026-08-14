"""Text workflow — a materialized view whose embedding column is a batch UDF.

Seed a synthetic product catalog, then build a **materialized view** over it
whose ``embedding`` column is computed by a batch sentence-transformers UDF at
refresh time. This is the other way to run a UDF in Geneva: the image/video/PDF
examples add a column to a table and *backfill* it, while here the UDF lives in a
view's projection and ``refresh`` is what executes it.

Everything runs offline in local mode — the catalog is generated on the client and
the embedding checkpoint (``all-MiniLM-L6-v2``, 384-dim) auto-downloads to the HF
cache on first use.

Order: **ingest-products -> enrich-products**.
"""

from __future__ import annotations

from geneva_examples.core.spec import (
    COMMON_HELP,
    Example,
    Step,
    params_from_signature,
)
from geneva_examples.examples.text import enrich, ingest

INGEST = Step(
    key="ingest-products",
    title="Seed a synthetic product catalog",
    description=(
        "Seed a `products` table with synthetic catalog rows — `product_id`, "
        "`title`, `description`, `category`, `word_count`. The rows are generated "
        "on the client from per-category vocabularies (no dataset download, no "
        "credentials), so descriptions in different categories are genuinely far "
        "apart in embedding space. Created with stable row IDs so the view built "
        "on top can refresh after the catalog grows; `--no-overwrite` appends "
        "instead of replacing, to give that refresh new rows to pick up."
    ),
    run=ingest.run,
    params=params_from_signature(
        ingest.run,
        help=COMMON_HELP
        | {
            "rows": "Products to generate.",
            "seed": "RNG seed — the same seed yields the same catalog.",
            "frag_size": "Rows per record batch (fragment granularity).",
            "overwrite": (
                "Drop the table first (default). Off = append the new rows to the "
                "existing catalog, renumbering product_id from its row count."
            ),
        },
        bounds={"rows": (1, None), "frag_size": (1, None)},
    ),
)

ENRICH = Step(
    key="enrich-products",
    title="Materialized view + batch embedding UDF",
    description=(
        "Build the `products_enriched_mv` materialized view over `products`: the "
        "view's `select` projects the catalog columns and adds an `embedding` "
        "column computed by a batch sentence-transformers UDF "
        "(all-MiniLM-L6-v2, 384-dim, L2-normalized, auto-downloads). "
        "`create_materialized_view` records the query and creates the view empty; "
        "`refresh` is what runs the UDF — on local Ray locally, on remote Geneva "
        "workers in enterprise mode. Finishes with a semantic search over the new "
        "vectors (`--no-search-demo` to skip loading the model on the driver). "
        "Because view rows are 1:1 with the source's, `--no-overwrite` refreshes "
        "the existing view and embeds only rows added since."
    ),
    run=enrich.run,
    gpu=True,
    requires="run ingest-products first",
    params=params_from_signature(
        enrich.run,
        help=COMMON_HELP
        | {
            "source_table": "Catalog table to read.",
            "view_name": "Materialized view to create/refresh.",
            "input_column": "Text column to embed.",
            "output_column": "Vector column the UDF writes into the view.",
            "model_name": "Sentence-transformers checkpoint (blank = all-MiniLM-L6-v2).",
            "dim": "Embedding width; 0 = infer from the model name.",
            "batch_size": "Descriptions per forward pass (auto-shrunk locally).",
            "device": "Torch device for the UDF (blank = auto: CUDA/MPS/CPU).",
            "concurrency": "Refresh concurrency — parallel actors (capped locally).",
            "source_task_size": "Source rows per refresh task (blank = geneva default).",
            "max_rows_per_fragment": "Rows per view fragment (blank = LanceDB default).",
            "overwrite": (
                "Drop and rebuild the view (default). Off = refresh the existing "
                "view, embedding only rows added since — keeps its original UDF."
            ),
            "search_demo": "Run a semantic search after the refresh.",
            "query_text": "Query for the search demo.",
        },
        bounds={"dim": (0, None), "batch_size": (1, None), "concurrency": (1, None)},
    ),
)

EXAMPLE = Example(
    name="text",
    title="Text embeddings via a materialized view",
    description=(
        "Seed a synthetic product catalog, then create a materialized view whose "
        "`embedding` column is a batch sentence-transformers UDF — the UDF runs on "
        "`refresh`, not as a column backfill — and search it semantically.\n\n"
        "Order: **ingest-products -> enrich-products**."
    ),
    modality="text",
    steps=(INGEST, ENRICH),
)
