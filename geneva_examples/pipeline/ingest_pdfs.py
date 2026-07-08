"""PDF ingest CLI: load local PDFs into the configured LanceDB table.

Reads every ``*.pdf`` under ``--pdf-dir`` and writes a ``pdfs`` table with a
``doc_id`` (string, the filename stem) and ``pdf_bytes`` (large_binary, the raw
PDF) column, ready for the ``chunk-pdfs`` stage. ``pdf_bytes`` is the column name
the ``extract_pages`` UDF binds to.
"""

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
    table_name: str = typer.Option("pdfs", help="Target table name."),
    pdf_dir: str = typer.Option(
        "./pdf_data", help="Local directory of *.pdf files to ingest."
    ),
    frag_size: int = typer.Option(
        1, help="PDFs per record batch (1 = one fragment per PDF)."
    ),
    overwrite: bool = typer.Option(
        True, help="Drop the table first if it already exists."
    ),
    table_write_retries: int = typer.Option(5, help="Retries for create/add ops."),
    table_write_retry_sleep_s: float = typer.Option(
        2.0, help="Base sleep (seconds) between table-write retries."
    ),
) -> None:
    """Load the PDFs under ``--pdf-dir`` into the table."""
    setup_logging(log_level)
    os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")

    import geneva

    from geneva_examples.core.utils.pdfs import load_pdf_batches

    cfg = load_config(config)
    if db_uri:
        cfg.db_uri = db_uri

    logger.info("geneva_version %s", geneva.__version__)
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
        "initial_sample %s",
        table.search().select(["doc_id"]).limit(5).to_list(),
    )
    logger.info("ingest_pdfs_ok")


if __name__ == "__main__":
    app()
