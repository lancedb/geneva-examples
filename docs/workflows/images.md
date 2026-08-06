# Images workflow

> Part of the geneva-examples docs — index: [docs/README.md](../README.md).

The images example ingests a small Hugging Face image dataset into an `images` table,
then backfills feature columns with Geneva UDFs: cheap CPU metadata, OpenCLIP
embeddings, and BLIP captions. It is the shortest end-to-end pipeline that runs on a
fresh clone (the pdf example has fewer steps but needs user-supplied PDFs), and it
runs in local mode with no configuration file.

## What this pipeline builds

| Step | Command | Column(s) | Compute |
|---|---|---|---|
| ingest-images | `uv run ingest-images` | creates the table: `image` (PNG bytes), `image_id`, `label`, `label_cat_dog` | driver only (CPU) |
| lightweight | `uv run lightweight` | `file_size` (int64), `dimensions` (struct<width, height>) | workers, CPU pool |
| embed | `uv run embed` | `embedding` (fixed_size_list<float32, 512>) | workers, GPU in enterprise mode / CPU in local mode |
| caption | `uv run caption` | `caption_blip` (string) | workers, GPU in enterprise mode / CPU in local mode |

All four steps target the `images` table (override with `--table-name`). Producing
code: `geneva_examples/examples/images/{ingest,lightweight,embed,caption}.py`; the
consolidated column reference is
[docs/reference/tables-and-schemas.md](../reference/tables-and-schemas.md).

## Run it

```sh
uv run ingest-images
uv run lightweight
uv run embed
uv run caption
```

Each step logs a canonical sentinel line on success — grep the output for these:

| Step | Success sentinel |
|---|---|
| ingest-images | `ingest_images_ok` |
| lightweight | `lightweight_ok` |
| embed | `embeddings_ok` |
| caption | `captions_ok` |

Note the sentinel names do not all match the command names (`embeddings_ok`, not
`embed_ok`). Sources: the final `logger.info` call in each step module under
`geneva_examples/examples/images/`.

## The search demo

After its backfill finishes, `embed` runs a text→image search demo on the driver: it
embeds `--query-text` with OpenCLIP and prints the top table matches. Enabled by
default; the demo — not the backfill — imports `open_clip` and `torch` on the driver
and downloads ViT-B-32 (`laion2b_s34b_b79k`) weights there. Pass `--no-search-demo`
to skip it entirely; the `embedding` backfill itself still runs remotely and needs
none of that on the driver. See `geneva_examples/examples/images/embed.py` for the
gating.

## Re-running

The three feature steps are destructive full recomputes. `lightweight`, `embed`, and
`caption` call `backfill_column` without a `reset` argument, and the helper defaults
to `reset=True`: if the column already exists it is dropped and every row is
recomputed, wiping prior values. None of these steps exposes a `--reset` flag, so
there is no incremental mode for them — re-running `caption` recomputes every
caption. `ingest-images` likewise drops and re-creates the table on a default re-run
(it has an `--overwrite` flag pair).

See `geneva_examples/core/backfill.py` for the authoritative reset-vs-incremental
contract and [docs/concepts/backfills.md](../concepts/backfills.md) for the
repo-wide table of which steps expose `--reset`. Neither mode may run while another
job is appending rows to the same table.

## Data source

`ingest-images` streams images from the Hugging Face dataset `timm/oxford-iiit-pet`
(split `train`), PNG-encodes each image, and writes small record batches with stable
row IDs enabled (`geneva_examples/core/utils/images.py`,
`geneva_examples/examples/images/ingest.py`). `--hf-dataset`, `--hf-split`, and
`--num-images` retarget or resize the load. The step sets
`HF_HOME=./huggingface_cache`, so dataset downloads land in the repo directory, not
your home cache.

## Full flag reference

Per-command flags, types, and defaults are generated from the step specs: see
[docs/reference/cli/images.md](../reference/cli/images.md).
