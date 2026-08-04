# PDF workflow

> Part of the geneva-examples docs — index: [docs/README.md](../README.md).

The pdf example loads local PDF files into a `pdfs` table, then backfills per-page
text and overlapping text chunks using the document UDFs geneva ships. `ingest-pdfs`
runs entirely on the driver; `chunk-pdfs` runs its two backfills on the workers'
CPU-only pool. No GPU is involved.

## What this pipeline builds

| Step | Command | Column | Type |
|---|---|---|---|
| ingest-pdfs | `uv run ingest-pdfs` | `doc_id` | string (file stem; duplicate stems get a `-N` suffix) |
| ingest-pdfs | | `pdf_bytes` | large_binary (the raw PDF) |
| chunk-pdfs | `uv run chunk-pdfs` | `pages` | list<struct{page_number: int32, text}> |
| chunk-pdfs | | `chunks` | list<struct{page_number: int32, chunk_id: int32, chunk}> |

The column names are load-bearing: geneva's shipped UDFs bind their input columns
by parameter name — `extract_pages` reads a column named `pdf_bytes` and
`chunk_pages` reads a column named `pages` — so the ingest table must expose
`pdf_bytes`, and `chunk-pdfs` must backfill `pages` before `chunks`
(`geneva_examples/examples/pdf/document.py`,
`geneva_examples/examples/pdf/chunk.py`). `chunks` splits each page's text with
LangChain's `RecursiveCharacterTextSplitter` at 2048 characters per chunk with 200
overlap — constants of the shipped `chunk_pages` UDF, verified against
geneva==0.14.1b5.

One silent-skip behavior to know (also verified against geneva==0.14.1b5):
`extract_pages` skips any PDF with more than 100 pages (its `MAX_PDF_PAGES` cap)
and any PDF it cannot parse — those rows get NULL `pages` and therefore NULL
`chunks` with no error, so watch the `null_pages`/`null_chunks` counts in the
backfill log. Pages with no extractable text are omitted from the `pages` list.

## Provide your own PDFs

The default input directory ships empty: `studio_data/pdfs/` is tracked with only a
`.gitkeep` file, so on a fresh clone `uv run ingest-pdfs` has nothing to load.
Copy your own PDFs into `studio_data/pdfs/` before running, or point `--pdf-dir` at
any directory containing `*.pdf` files.

What a missing corpus looks like (`geneva_examples/core/utils/pdfs.py`,
`geneva_examples/examples/pdf/ingest.py`):

| Situation | Error |
|---|---|
| `--pdf-dir` does not exist | `FileNotFoundError: no PDF directory at <dir>` |
| Directory exists but holds no `*.pdf` (the fresh-clone case; dotfiles such as `.gitkeep` are ignored) | `FileNotFoundError: no .pdf files in <dir>` |
| Loading yields zero batches (ingest-level backstop) | `RuntimeError(f"no PDFs loaded from {pdf_dir}")` — with the default directory: `no PDFs loaded from ./studio_data/pdfs` |

## Run it

```sh
cp ~/some/dir/*.pdf studio_data/pdfs/
uv run ingest-pdfs
uv run chunk-pdfs
```

Success sentinels to grep for: `ingest_pdfs_ok` and `pdf_chunks_ok`. Re-running
`chunk-pdfs` destructively recomputes both columns — it has no `--reset` flag, and
the shared backfill helper defaults to drop-and-recompute (see
[docs/concepts/backfills.md](../concepts/backfills.md)). Re-running `ingest-pdfs`
drops and re-creates the table by default (`--overwrite` flag pair).

## Reusing geneva's shipped UDFs

Unlike the other examples, the pdf step defines no UDF bodies: it adopts geneva's
pre-built `geneva.udfs.document.extract_pages` and `chunk_pages` via
`attrs.evolve(...)`, swapping in this repo's pinned manifest and a fresh `version`
so each run re-materializes. See `geneva_examples/examples/pdf/document.py` for the
authoritative adoption pattern, and
[docs/reference/worker-runtime-pins.md](../reference/worker-runtime-pins.md) for
the worker package pins it applies.

## Full flag reference

Per-command flags, types, and defaults are generated from the step specs: see
[docs/reference/cli/pdf.md](../reference/cli/pdf.md).
