"""PDF chunk-extraction pipeline — a self-contained example.

Load local PDFs, then backfill nested-list `pages` and `chunks` columns using
Geneva's pre-built `geneva.udfs.document` UDFs (per-page text + overlapping
text windows). Both steps run on the CPU pool.
"""

from __future__ import annotations

from geneva_examples.core.spec import (
    COMMON_HELP,
    Example,
    Step,
    params_from_signature,
)
from geneva_examples.examples.pdf import chunk, ingest

INGEST = Step(
    key="ingest-pdfs",
    title="Ingest PDFs",
    description=(
        "Load every `*.pdf` under `--pdf-dir` into a `pdfs` table (`doc_id` + "
        "`pdf_bytes`), one fragment per PDF."
    ),
    run=ingest.run,
    params=params_from_signature(
        ingest.run,
        help=COMMON_HELP | {"pdf_dir": "Local directory of *.pdf files to ingest."},
    ),
)

CHUNK = Step(
    key="chunk-pdfs",
    title="Extract pages + chunks",
    description=(
        "Backfill `pages` (per-page text via pypdf) then `chunks` (overlapping "
        "windows via LangChain's RecursiveCharacterTextSplitter). Order matters — "
        "`chunks` reads the `pages` column. CPU pool."
    ),
    run=chunk.run,
    requires="run ingest-pdfs first",
    params=params_from_signature(
        chunk.run,
        help=COMMON_HELP
        | {
            "backfill_concurrency": "Backfill concurrency (1 locally).",
            "backfill_task_size": "Backfill task size.",
            "backfill_checkpoint_size": "Backfill checkpoint size.",
            "backfill_flush_interval_s": "Batch checkpoint flush interval (seconds).",
        },
    ),
)

EXAMPLE = Example(
    name="pdf",
    title="PDF chunk-extraction pipeline",
    description=(
        "Load your own PDFs, then extract per-page text and overlapping text "
        "chunks with Geneva's pre-built document UDFs — ready to embed or explode "
        "into a per-chunk table.\n\nOrder: **ingest-pdfs → chunk-pdfs**."
    ),
    modality="pdf",
    steps=(INGEST, CHUNK),
)
