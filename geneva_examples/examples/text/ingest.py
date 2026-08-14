"""Products ingest CLI: seed a synthetic product catalog.

Writes a ``products`` table with ``product_id``, ``title``, ``description``,
``category`` and ``word_count`` — the source rows the ``products_enriched_mv``
materialized view reads. The catalog is generated locally (see
:mod:`geneva_examples.examples.text.products`), so there is no dataset download,
no credentials, and nothing to place on disk first.

The table is created with stable row IDs, which is what lets the view refresh
after the catalog grows (see ``OPT_STABLE_ROW_IDS`` in ``core.common``).
"""

from __future__ import annotations

import logging
import os

from geneva_examples.core.common import OPT_STABLE_ROW_IDS, connect, format_sample
from geneva_examples.core.config import Config
from geneva_examples.core.utils.retry import retry_io
from geneva_examples.examples.text.products import generate_products, to_batches

logger = logging.getLogger(__name__)


def run(
    cfg: Config,
    *,
    table_name: str = "products",
    rows: int = 200,
    seed: int = 42,
    frag_size: int = 50,
    overwrite: bool = True,
    table_write_retries: int = 5,
    table_write_retry_sleep_s: float = 2.0,
) -> None:
    """Seed ``rows`` synthetic products into ``table_name``.

    With ``--no-overwrite`` the rows are **appended** to an existing table
    instead of replacing it, which is how you give the view new source rows to
    pick up on its next refresh (``embed-descriptions --no-overwrite``).
    """
    os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")

    import geneva

    logger.info("geneva_version %s mode %s", geneva.__version__, cfg.mode)
    logger.info(
        "db_uri %s table %s rows %s seed %s", cfg.db_uri, table_name, rows, seed
    )

    conn = connect(cfg)

    products = generate_products(rows, seed=seed)
    batches = to_batches(products, frag_size=frag_size)

    if overwrite:
        try:
            conn.drop_table(table_name)
            logger.info("dropped_existing_table %s", table_name)
        except Exception:  # noqa: BLE001
            pass

    existing = None
    if not overwrite:
        try:
            existing = conn.open_table(table_name)
        except Exception:  # noqa: BLE001 - absent table: fall through to create
            existing = None

    if existing is not None:
        # Appending to a table the view already tracks: keep the offset unique so
        # re-running with the same --rows doesn't collide on product_id.
        offset = existing.count_rows()
        products = [
            {**row, "product_id": f"prod-{offset + i:05d}"}
            for i, row in enumerate(products)
        ]
        batches = to_batches(products, frag_size=frag_size)
        for batch_index, batch in enumerate(batches, start=1):
            retry_io(
                f"add_batch_{batch_index}",
                lambda batch=batch: existing.add(batch),
                attempts=table_write_retries,
                sleep_s=table_write_retry_sleep_s,
            )
        table = existing
        logger.info("appended_rows %s", len(products))
    else:
        table = retry_io(
            "create_table",
            lambda: conn.create_table(
                table_name,
                data=batches[0],
                # Required for the view to refresh once the catalog moves; see
                # OPT_STABLE_ROW_IDS.
                storage_options={OPT_STABLE_ROW_IDS: "true"},
            ),
            attempts=table_write_retries,
            sleep_s=table_write_retry_sleep_s,
        )
        for batch_index, batch in enumerate(batches[1:], start=2):
            retry_io(
                f"add_batch_{batch_index}",
                lambda batch=batch: table.add(batch),
                attempts=table_write_retries,
                sleep_s=table_write_retry_sleep_s,
            )

    logger.info("rows_total %s", table.count_rows())
    logger.info(
        "initial_sample\n%s",
        format_sample(
            table.search()
            .select(["product_id", "title", "category", "word_count"])
            .limit(5)
            .to_list()
        ),
    )
    logger.info("ingest_products_ok")
