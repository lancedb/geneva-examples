"""PDF chunk stage: extract per-page text, then split it into text chunks (CPU).

Backfills two nested-list columns onto the ``pdfs`` table using Geneva's
pre-built document UDFs (see :mod:`geneva_examples.examples.pdf.document`):

  - ``pages``  = ``extract_pages(pdf_bytes)``  -> list<{page_number, text}>
  - ``chunks`` = ``chunk_pages(pages)``        -> list<{page_number, chunk_id, chunk}>

``chunks`` is the chunk-extraction output: each PDF row carries its overlapping
text windows (``RecursiveCharacterTextSplitter``, 2048 chars / 200 overlap),
ready for a downstream embedding stage or an explode into a per-chunk table.
"""

from __future__ import annotations

import logging

from geneva_examples.core.backfill import backfill_column
from geneva_examples.core.common import (
    build_manifest,
    connect,
    format_sample,
    runtime_session,
)
from geneva_examples.core.config import Config

logger = logging.getLogger(__name__)


def run(
    cfg: Config,
    *,
    table_name: str = "pdfs",
    backfill_timeout_min: int = 1000,
    backfill_concurrency: int = 32,
    backfill_task_size: int = 256,
    backfill_checkpoint_size: int = 128,
    backfill_flush_interval_s: float = 30.0,
    use_cpu_only_pool: bool = True,
    schema_wait_attempts: int = 30,
    schema_wait_sleep_s: int = 2,
) -> None:
    """Add ``pages`` + ``chunks`` columns to the configured PDFs table."""
    import geneva

    from geneva_examples.examples.pdf.document import (
        PDF_RUNTIME_PIP,
        build_chunk_pages_udf,
        build_extract_pages_udf,
    )

    logger.info("geneva_version %s mode %s", geneva.__version__, cfg.mode)
    logger.info("db_uri %s table %s", cfg.db_uri, table_name)

    conn = connect(cfg)
    table = conn.open_table(table_name)

    manifest = build_manifest(cfg, "pdf", PDF_RUNTIME_PIP)
    # Order matters: `extract_pages` reads `pdf_bytes` and produces `pages`;
    # `chunk_pages` then reads that `pages` column. Both bind their input columns
    # by parameter name, so the column names here are load-bearing.
    columns = {
        "pages": build_extract_pages_udf(manifest=manifest),
        "chunks": build_chunk_pages_udf(manifest=manifest),
    }
    with runtime_session(conn, cfg):
        for column, udf in columns.items():
            table = backfill_column(
                conn=conn,
                table=table,
                table_name=table_name,
                column=column,
                udf=udf,
                concurrency=backfill_concurrency,
                task_size=backfill_task_size,
                checkpoint_size=backfill_checkpoint_size,
                flush_interval_s=backfill_flush_interval_s,
                timeout_min=backfill_timeout_min,
                wait_attempts=schema_wait_attempts,
                wait_sleep_s=schema_wait_sleep_s,
                use_cpu_only_pool=use_cpu_only_pool,
            )

    logger.info(
        "chunk_sample\n%s",
        format_sample(table.search().select(["doc_id", "chunks"]).limit(5).to_list()),
    )
    logger.info("pdf_chunks_ok")
