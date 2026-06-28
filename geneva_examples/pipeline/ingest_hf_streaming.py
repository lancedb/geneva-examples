"""Streaming HF ingest CLI: bounded, credential-re-vending load into LanceDB.

Replaces the one-shot ``tbl.add(staged)`` that dies with S3 ``ExpiredToken`` on
long ``db://`` loads (GEN-666) with many bounded ``append`` sub-writes, re-vending
fresh STS credentials before each chunk. See
``geneva_examples.core.utils.hf_streaming`` for the levers.
"""

from __future__ import annotations

import itertools
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
    hf_dataset: str = typer.Option(
        "cornell-movie-review-data/rotten_tomatoes",
        help="Hugging Face dataset (namespace/name repo id).",
    ),
    hf_split: str = typer.Option(
        "train",
        help="Dataset split (datasets mode only; parquet mode reads all shards).",
    ),
    limit: int | None = typer.Option(
        None, help="Cap total source rows ingested (default: whole dataset)."
    ),
    chunk_rows: int = typer.Option(
        50_000, help="Bounded sub-write size: rows per append."
    ),
    source_mode: str = typer.Option(
        "datasets",
        help="Source reader: 'datasets' (general) or 'parquet' (hf:// scale).",
    ),
    revend_mode: str = typer.Option(
        "connect",
        help="Credential re-vend lever per chunk: 'connect' (guaranteed), "
        "'reopen' (lighter), or 'latest' (in-place refresh).",
    ),
    overwrite: bool = typer.Option(
        True, help="Drop the table first if it already exists."
    ),
    resume: bool = typer.Option(
        False, help="Resume: skip rows already in the table (requires --no-overwrite)."
    ),
    table_write_retries: int = typer.Option(5, help="Retries for create/add ops."),
    table_write_retry_sleep_s: float = typer.Option(
        2.0, help="Base sleep (seconds) between table-write retries."
    ),
) -> None:
    """Stream a Hugging Face dataset into the table in bounded, re-vended chunks."""
    setup_logging(log_level)
    os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")
    os.environ.setdefault("HF_HOME", "./huggingface_cache")

    import geneva

    from geneva_examples.core.utils.hf_streaming import (
        fresh_table,
        iter_hf_batches,
        vended_token_prefix,
    )

    if resume and overwrite:
        raise typer.BadParameter(
            "--resume conflicts with --overwrite; pass --no-overwrite"
        )

    cfg = load_config(config)
    if db_uri:
        cfg.db_uri = db_uri
    if table_name:
        cfg.table_name = table_name

    logger.info("geneva_version %s", geneva.__version__)
    logger.info("db_uri %s table %s", cfg.db_uri, cfg.table_name)
    logger.info(
        "hf_dataset %s hf_split %s source_mode %s revend_mode %s chunk_rows %d limit %s",
        hf_dataset,
        hf_split,
        source_mode,
        revend_mode,
        chunk_rows,
        limit,
    )

    if cfg.hf_token:
        os.environ.setdefault("HF_TOKEN", cfg.hf_token)

    conn = connect(cfg)

    # Decide table setup: create fresh, append to existing, or resume.
    existing = cfg.table_name in conn.table_names()
    skip_rows = 0
    need_create = True
    if resume:
        if not existing:
            raise RuntimeError(
                f"--resume set but table {cfg.table_name} does not exist"
            )
        need_create = False
        skip_rows = conn.open_table(cfg.table_name).count_rows()
        logger.info("resume_skip_rows %d", skip_rows)
    elif overwrite and existing:
        try:
            conn.drop_table(cfg.table_name)
            logger.info("dropped_existing_table %s", cfg.table_name)
        except Exception:  # noqa: BLE001
            pass
    elif existing:
        need_create = False

    batches = iter_hf_batches(
        hf_dataset,
        hf_split,
        chunk_rows,
        limit=limit,
        skip_rows=skip_rows,
        mode=source_mode,
        hf_token=cfg.hf_token,
    )

    # Create from the first batch's schema, then append every chunk (incl. the
    # first) through the same bounded + re-vended + retried loop.
    if need_create:
        first = next(batches, None)
        if first is None:
            raise RuntimeError("no rows from source dataset")
        retry_io(
            "create_table",
            lambda: conn.create_table(cfg.table_name, schema=first.schema),
            attempts=table_write_retries,
            sleep_s=table_write_retry_sleep_s,
        )
        batches = itertools.chain([first], batches)

    table = None
    rows = 0
    for chunk_index, batch in enumerate(batches):
        # Re-vend fresh credentials before each bounded append.
        conn, table = fresh_table(
            cfg, cfg.table_name, mode=revend_mode, conn=conn, table=table
        )
        retry_io(
            f"add_chunk_{chunk_index}",
            lambda b=batch, t=table: t.add(b, mode="append"),
            attempts=table_write_retries,
            sleep_s=table_write_retry_sleep_s,
        )
        rows += batch.num_rows
        logger.info(
            "chunk %d rows=%d total=%d token=%s",
            chunk_index,
            batch.num_rows,
            rows,
            vended_token_prefix(table),
        )

    if table is None:
        logger.info("no_chunks_ingested rows_added 0")
    else:
        logger.info("rows_added %d table_rows %s", rows, table.count_rows())
    logger.info("ingest_hf_streaming_ok")


if __name__ == "__main__":
    app()
