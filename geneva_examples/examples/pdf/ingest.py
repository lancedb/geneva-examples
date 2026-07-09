"""PDF ingest CLI: load local PDFs into the configured LanceDB table.

Reads every ``*.pdf`` under ``--pdf-dir`` and writes a ``pdfs`` table with a
``doc_id`` (string, the filename stem) and ``pdf_bytes`` (large_binary, the raw
PDF) column, ready for the ``chunk-pdfs`` stage. ``pdf_bytes`` is the column name
the ``extract_pages`` UDF binds to.
"""

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
    table_name: str = "pdfs",
    pdf_dir: str = "./studio_data/pdfs",
    frag_size: int = 1,
    overwrite: bool = True,
    table_write_retries: int = 5,
    table_write_retry_sleep_s: float = 2.0,
) -> None:
    """Load the PDFs under ``--pdf-dir`` into the table."""
    os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")

    import geneva

    from geneva_examples.core.utils.pdfs import load_pdf_batches

    logger.info("geneva_version %s mode %s", geneva.__version__, cfg.mode)
    logger.info("db_uri %s table %s pdf_dir %s", cfg.db_uri, table_name, pdf_dir)

    conn = connect(cfg)

    pdf_batches = load_pdf_batches(pdf_dir, frag_size=frag_size)
    if not pdf_batches:
        raise RuntimeError(f"no PDFs loaded from {pdf_dir}")

    if overwrite:
        try:
            conn.drop_table(table_name)
            logger.info("dropped_existing_table %s", table_name)
        except Exception:  # noqa: BLE001
            pass

    table = retry_io(
        "create_table",
        lambda: conn.create_table(table_name, data=pdf_batches[0]),
        attempts=table_write_retries,
        sleep_s=table_write_retry_sleep_s,
    )
    total_batches = len(pdf_batches)
    for batch_index, batch in enumerate(pdf_batches[1:], start=2):
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
        format_sample(table.search().select(["doc_id"]).limit(5).to_list()),
    )
    logger.info("ingest_pdfs_ok")
