"""Enrich step: a materialized view whose ``embedding`` column is a batch UDF.

This is the materialized-view path, not the column-backfill path the other
feature stages take. Instead of adding a column to ``products`` and backfilling
it, the step builds a **query** over the catalog whose projection mixes plain
columns with one UDF-computed column::

    query = table.search(None).select({
        "product_id":  "product_id",
        "title":       "title",
        "description": "description",
        "category":    "category",
        "word_count":  "word_count",
        "embedding":   EmbedDescription(),   # <- the batch UDF
    })
    db.create_materialized_view("products_enriched_mv", query)
    view.refresh(concurrency=4)

``create_materialized_view`` records the query (including the marshalled UDF) and
creates the view **empty**; ``refresh`` is what actually runs the UDF — on local
Ray in local mode, on the remote Geneva workers in enterprise mode — and commits
the rows. The source table keeps only its original five columns: the vectors live
in the view, so re-embedding with another checkpoint means rebuilding a view, not
rewriting the catalog.

A ``str`` value in that projection is a SQL expression, not just a column name,
so derived scalars can be computed in the same pass — this example keeps
``word_count`` as a plain projection because the ingest step already counted it.

Because the view's rows are 1:1 with the source's, ``refresh`` is incremental:
run ``ingest-products --no-overwrite`` to append products, then this step with
``--no-overwrite`` to embed only what was added.
"""

from __future__ import annotations

import logging
import os

from geneva_examples.core.common import (
    build_manifest,
    connect,
    format_sample,
    local_concurrency,
    local_or,
    require_stable_row_ids,
    resolve_resources,
    runtime_session,
)
from geneva_examples.core.config import Config
from geneva_examples.core.utils.retry import retry_io

logger = logging.getLogger(__name__)

# Projected through from the catalog, in the view's column order. `embedding` is
# appended as the UDF-computed column.
PASSTHROUGH_COLUMNS = ("product_id", "title", "description", "category", "word_count")


def run(
    cfg: Config,
    *,
    source_table: str = "products",
    view_name: str = "products_enriched_mv",
    input_column: str = "description",
    output_column: str = "embedding",
    model_name: str = "",
    dim: int = 0,
    batch_size: int = 64,
    device: str = "",
    num_cpus: float = 2.0,
    num_gpus: float | None = None,
    memory_gib: int = 1,
    checkpoint_size: int = 512,
    task_size: int = 512,
    concurrency: int = 4,
    source_task_size: int | None = None,
    max_rows_per_fragment: int | None = None,
    overwrite: bool = True,
    search_demo: bool = True,
    query_text: str = "a warm layer for hiking in cold weather",
    table_write_retries: int = 5,
    table_write_retry_sleep_s: float = 2.0,
) -> None:
    """Create ``view_name`` over ``source_table`` and refresh it to embed rows."""
    os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")
    os.environ.setdefault("HF_HOME", "./huggingface_cache")

    import geneva

    from geneva_examples.examples.text.embed_description import (
        DEFAULT_MODEL,
        SENTENCE_TRANSFORMERS_RUNTIME_PIP,
        build_embed_description_udf,
        resolve_dim,
    )

    model_name = model_name or DEFAULT_MODEL
    # The vector column declares its width when the view is created, before any
    # worker has loaded the model — so resolve it here and let the UDF's setup()
    # verify it against the real checkpoint.
    dim = resolve_dim(model_name, dim)

    resolved_gpus = num_gpus if num_gpus is not None else 0.5
    num_cpus, resolved_gpus, memory_bytes = resolve_resources(
        cfg, num_cpus=num_cpus, num_gpus=resolved_gpus, memory_gib=memory_gib
    )
    batch_size = local_or(cfg, min(batch_size, 16), batch_size)

    logger.info("geneva_version %s mode %s", geneva.__version__, cfg.mode)
    logger.info(
        "db_uri %s source %s view %s model %s dim %s",
        cfg.db_uri,
        source_table,
        view_name,
        model_name,
        dim,
    )

    conn = connect(cfg)
    src = conn.open_table(source_table)
    # A view can only refresh once the source version has moved if the source has
    # stable row IDs — and the maintenance agent's compaction moves it unprompted.
    # Check before creating the view, not on the refresh that fails months later.
    require_stable_row_ids(src, source_table)

    view = None
    if overwrite:
        try:
            conn.drop_table(view_name)
            logger.info("dropped_existing_view %s", view_name)
        except Exception:  # noqa: BLE001
            pass
    else:
        # Incremental: reuse the existing view so refresh only embeds the source
        # rows added since the last one. A view keeps the UDF it was created with
        # (which is why no UDF is built on this path) — to change the model,
        # rebuild the view by dropping --no-overwrite.
        try:
            view = conn.open_table(view_name)
            logger.info("refreshing_existing_view %s", view_name)
        except Exception:  # noqa: BLE001 - absent view: create it below
            view = None

    if view is None:
        manifest = build_manifest(cfg, "text-embed", SENTENCE_TRANSFORMERS_RUNTIME_PIP)
        udf = build_embed_description_udf(
            input_column=input_column,
            manifest=manifest,
            model_name=model_name,
            dim=dim,
            batch_size=batch_size,
            device=device,
            num_cpus=num_cpus,
            num_gpus=resolved_gpus,
            memory_bytes=memory_bytes,
            checkpoint_size=checkpoint_size,
            task_size=task_size,
        )
        # The projection: plain columns by name, plus the UDF for the embedding.
        # Keys are the view's output column names.
        projection: dict[str, object] = {name: name for name in PASSTHROUGH_COLUMNS}
        projection[output_column] = udf
        query = src.search(None).select(projection)
        view = retry_io(
            "create_materialized_view",
            lambda: conn.create_materialized_view(view_name, query),
            attempts=table_write_retries,
            sleep_s=table_write_retry_sleep_s,
        )
        logger.info("created_view %s columns %s", view_name, view.schema.names)

    refresh_kwargs: dict = {}
    if source_task_size is not None:
        refresh_kwargs["source_task_size"] = source_task_size
    if max_rows_per_fragment is not None:
        refresh_kwargs["max_rows_per_fragment"] = max_rows_per_fragment
    if cfg.is_local:
        # Local Ray only has this machine's cores: cap the actor count and skip
        # the admission pre-flight so tasks queue instead of being rejected.
        concurrency = local_concurrency(concurrency)
        refresh_kwargs["_admission_check"] = False

    logger.info("refreshing %s concurrency %s", view_name, concurrency)
    with runtime_session(conn, cfg):
        # This is where the UDF runs — one model load per actor, then
        # batch_size-sized forward passes over each task's descriptions.
        view.refresh(concurrency=concurrency, **refresh_kwargs)
    view.checkout_latest()

    logger.info("view_rows %s", view.count_rows())
    logger.info("view_columns %s", view.schema.names)
    logger.info(
        "view_sample\n%s",
        format_sample(
            view.search()
            .select(["product_id", "category", "word_count", output_column])
            .limit(5)
            .to_list()
        ),
    )

    # Semantic search over the view. Gated behind --search-demo because it loads
    # the model on the *driver* to encode the query — the refresh itself needs no
    # driver-side model.
    if search_demo:
        from geneva_examples.examples.text.embed_description import encode_query

        query_vector = encode_query(query_text, model_name=model_name)
        rows = view.search(query_vector, output_column).limit(5).to_list()
        logger.info("search_query %r matches %s", query_text, len(rows))
        logger.info(
            "search_results\n%s",
            format_sample(
                [
                    {
                        "product_id": r.get("product_id"),
                        "category": r.get("category"),
                        "title": r.get("title"),
                        "_distance": r.get("_distance"),
                    }
                    for r in rows
                ]
            ),
        )

    logger.info("enrich_products_ok")
