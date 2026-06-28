# geneva-examples — Geneva remote UDF examples

A self-contained set of **example UDFs** and the **submission tooling** to run
them against LanceDB Cloud + a remote Geneva runtime. Point it at your Geneva
host, fill in three config values, and run a backfill.

What's here:

1. **Reusable Geneva UDFs** (in [`geneva_examples/udfs/`](geneva_examples/udfs/)):
   - `imageinfo` — lightweight CPU UDFs: byte size + image dimensions
   - `clip` — OpenCLIP image embeddings
   - `blip` — BLIP image captions
   - `openpose` — OpenPose pose-skeleton PNGs
   - `chunkers` — video-chunking UDTFs (split videos into fixed-length clips)
2. **Pipeline CLIs** that ingest data and submit the UDFs as Geneva backfills.
3. **Two inspection CLIs** — `stats` and `jobs` — that read table/job state over
   the same connection.
4. **UDF Studio** — a Gradio app for prototyping UDFs/chunkers locally before
   wiring them into a stage (see below).

The UDF bodies are self-contained closures: their imports and helpers are
nested inside the factory so they ship to the remote Geneva workers via the
pinned pip manifests and run there. The driver/CLI code stays lightweight.

## Requirements

- Python ≥ 3.12 and [`uv`](https://docs.astral.sh/uv/).
- A LanceDB Cloud API key + region, and a reachable Geneva host URL.
- A GPU-backed Geneva runtime for the embed/caption/openpose stages — those
  models run **remotely** in the Geneva workers, not on your machine.

## Install

```bash
uv sync
```

`geneva`, `lancedb`, and `pylance` are pinned betas served from public Gemfury
indexes (declared in [`pyproject.toml`](pyproject.toml)); `uv` resolves them
automatically — no extra flags.

## Configure

All configuration lives in a single YAML file — there is no environment-variable
fallback.

```bash
cp config-example.yaml config.yaml
# edit config.yaml
```

`config.yaml` is gitignored; `config-example.yaml` is the tracked template.

| Key               | Required | Default           | Description                                  |
| ----------------- | -------- | ----------------- | -------------------------------------------- |
| `lancedb_api_key` | **yes**  | —                 | LanceDB Cloud API key.                       |
| `lancedb_region`  | **yes**  | —                 | LanceDB Cloud region.                        |
| `geneva_host`     | **yes**  | —                 | Reachable Geneva runtime URL (load balancer).|
| `db_uri`          | no       | `db://quickstart` | Database URI, shared by every CLI.           |
| `table_name`      | no       | `images`          | Table name, shared by every CLI.             |
| `s3_*`            | no       | —                 | S3 storage creds (all four or none).         |
| `hf_token`        | no       | —                 | Hugging Face token (raises HF rate limits).  |

A missing `config.yaml`, or one missing any required field, fails with a clear
error.

## Image workflow

```bash
uv run ingest-images   # create the table + load images from a Hugging Face dataset
uv run lightweight     # backfill file_size + dimensions (CPU)
uv run embed           # backfill OpenCLIP embeddings + a text-to-image search demo (GPU)
uv run caption         # backfill two BLIP caption variants (GPU)
```

## Video workflow

```bash
uv run ingest-videos   # download MP4s into the `videos` table
uv run chunk-videos    # split into fixed-length clips + start frame -> `video_clips`
uv run frame-embed     # OpenCLIP embedding on each clip's frame
uv run frame-caption   # BLIP caption on each clip's frame
uv run frame-openpose  # OpenPose pose-skeleton PNG on each clip's frame
uv run cleanup         # drop the `videos` + `video_clips` tables
```

There is also an OpenVid variant (`ingest-videos-openvid` → `chunk-videos-openvid`)
that registers reference-only rows and chunks by reading the blob from the source
dataset, plus `seed-video-clips` for load-testing the frame stages without a full
chunk run. Run any CLI with `--help` for its options (e.g. `--chunk-seconds`,
`--model-name`/`--pretrained`/`--dim` on `frame-embed`).

## Streaming ingest

For a **large** dataset on a `db://` enterprise connection, the naive one-shot
load dies after ~1h with S3 `ExpiredToken`:

```python
# Anti-pattern: one Arrow dataset over the whole repo, one long write.
staged = ds.dataset("~/datacomp_meta", format="parquet")  # ~3TB metadata
tbl.add(staged)   # single Lance write outlives the vended STS token -> ExpiredToken
```

The table is opened once, the vended STS credentials are baked into the Lance
backend, and that single write can't finish inside the token's lifetime.
`ingest-hf-streaming` is the copy-pasteable mitigation: it **streams the source
in `--chunk-rows` batches** (so no single write outlives the TTL) and **re-vends
fresh credentials before every chunk** (so late chunks never use a stale token).

```bash
# zero-config demo (datasets mode, small public text set)
uv run ingest-hf-streaming --limit 1000 --chunk-rows 200

# scale path: stream datacomp_xlarge parquet metadata straight from hf://
# (no local download) into a db:// table, re-vending creds every chunk.
HF_TOKEN=... uv run ingest-hf-streaming \
    --source-mode parquet --hf-dataset mlfoundations/datacomp_xlarge \
    --db-uri db://training-playbook --table-name datacomp_xlarge \
    --chunk-rows 50000 --revend-mode connect

# restart a failed load: skip rows already written, append the rest
uv run ingest-hf-streaming --resume --no-overwrite --table-name datacomp_xlarge ...
```

### Options

Run `uv run ingest-hf-streaming --help` for the live list. All options:

| Option | Default | Description |
| ------ | ------- | ----------- |
| `--config` | `./config.yaml` | Path to the config YAML. |
| `--log-level` | `INFO` | Python logging level (`DEBUG`/`INFO`/`WARNING`/…). |
| `--db-uri` | config `db_uri` | Override the database URI (e.g. `db://training-playbook` or a local path). |
| `--table-name` | config `table_name` | Override the target table name. |
| `--hf-dataset` | `cornell-movie-review-data/rotten_tomatoes` | Hugging Face dataset, as a `namespace/name` repo id. |
| `--hf-split` | `train` | Dataset split — **datasets mode only**; parquet mode reads every shard. |
| `--limit` | _none (whole dataset)_ | Cap on total **source** rows ingested. Counts skipped rows too, so resuming a `--limit N` load tops the table up to `N` total. |
| `--chunk-rows` | `50000` | Bounded sub-write size: rows per `append`. Size it so each append finishes well under the STS TTL. |
| `--source-mode` | `datasets` | Source reader — `datasets` or `parquet` (see below). |
| `--revend-mode` | `connect` | Per-chunk credential re-vend lever — `connect`, `reopen`, or `latest` (see below). |
| `--overwrite` / `--no-overwrite` | `--overwrite` | Drop the table first if it exists. Mutually exclusive with `--resume`. |
| `--resume` / `--no-resume` | `--no-resume` | Skip rows already in the table and append the rest. Requires `--no-overwrite`. |
| `--table-write-retries` | `5` | Attempts for each create/add op. |
| `--table-write-retry-sleep-s` | `2.0` | Base sleep (seconds) between retries; backoff is linear (`sleep × attempt`). |

For gated datasets, set `hf_token` in `config.yaml` (exported to `HF_TOKEN`) or
pass `HF_TOKEN=...` in the environment.

**`--source-mode`**

| Value | Behavior |
| ----- | -------- |
| `datasets` (default) | `load_dataset(..., streaming=True)` buffered into batches. Works for any `datasets`-streamable repo whose columns are Arrow-serializable scalars (text/metadata). Honors `--hf-split`. Decode-heavy feature types (PIL images, audio) are out of scope. |
| `parquet` | Streams parquet straight from `hf://` via `HfFileSystem` + `pyarrow.dataset` — exact schema, no decode, no full download. Scales to datacomp-style sharded metadata. Reads **all** `*.parquet` in the repo (ignores `--hf-split`). |

**`--revend-mode`** — each chunk logs the vended `aws_session_token` prefix
(`token=...`) so you can confirm credentials actually rotate.

| Value | Behavior |
| ----- | -------- |
| `connect` (default) | Reconstructs the connection (`geneva.connect`) before each chunk → fresh namespace client → fresh `describe_table` vend. The only lever **guaranteed** to re-vend. |
| `reopen` | Reuses the connection and re-opens the table (`conn.open_table`) per chunk. Lighter, but whether it re-vends happens inside opaque native code and is unconfirmed. |
| `latest` | Holds one table and refreshes its credentials in place via the underlying `latest_storage_options()` vend primitive (a private, internal lancedb API). |

> **Tuning `--chunk-rows`.** 50k metadata rows finishes far inside a ~1h STS TTL.
> For wide rows or rows carrying blobs, lower it so each append stays comfortably
> under the TTL.

## Inspecting state

```bash
uv run stats              # summarize tables: row counts, schema, populated feature columns
uv run jobs               # list active (PENDING/RUNNING) Geneva backfill jobs
uv run jobs --all         # include DONE/FAILED/CANCELLED
uv run jobs kill <job_id> # cancel a Geneva job by id
```

Both connect via `config.yaml` (override with `--config`/`--db-uri`).

## UDF Studio

A Gradio app for prototyping UDFs and chunkers before wiring them into a stage.
Pick a template, point it at sample data on disk, and run your function
**locally on the driver** (no Ray, GPU, or cluster) to see its output.

```bash
uv run udf-studio                 # http://127.0.0.1:7860, samples from ./studio_data
uv run udf-studio --data-dir ~/my-samples --library ~/udf-lib --host 0.0.0.0
```

- **Contract.** A UDF defines `transform(value)` (one input → one output); a
  chunker defines `chunk(value)` that yields one `dict` per output row. Code at
  module level runs once per Run, so load models there.
- **Sample data** comes from `--data-dir` (default `studio_data/`): drop files
  into `images/`, `videos/`, `audio/`, or rows into `input.csv` (text). See
  [`studio_data/README.md`](studio_data/README.md). The sample media itself is
  gitignored — add your own.
- **Library.** Save/load work-in-progress to a local LanceDB at `--library`
  (default `udf_library/`).
- It never builds a manifest or submits to the cluster — promoting a finished
  function to a `geneva_examples/udfs/` factory + a stage CLI stays a manual step.

## Development

```bash
make install   # sync deps + install the pre-commit hook
make check     # lint + format-check + tests (the CI gate)
make test      # pytest with coverage
```

Tests exercise the UDF manifests, the pure helpers, config loading, and the
`stats`/`jobs` formatting helpers.
