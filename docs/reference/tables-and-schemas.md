# Tables and schemas

> Part of the geneva-examples docs — index: [docs/README.md](../README.md).

Column-by-column reference for every table the examples produce, plus the two
geneva system tables. Types marked *inferred* come from tables created with
`RecordBatch.from_pylist` and no explicit schema, so PyArrow infers them from the
Python values. Default table names are declared per step (never in config); the
workflow pages show how each table is built.

## Contents

- [`images`](#images)
- [`videos`](#videos)
- [`video_clips`](#video_clips)
- [`pdfs`](#pdfs)
- [`audio`](#audio)
- [`debug_demo`](#debug_demo)
- [System tables: geneva_jobs and geneva_errors](#system-tables-geneva_jobs-and-geneva_errors)
- [Creation invariants](#creation-invariants)

## `images`

Created by `ingest-images` (`geneva_examples/examples/images/ingest.py`, rows
built by `geneva_examples/core/utils/images.py:load_hf_image_batches`); feature
columns added by the later steps. See
[docs/workflows/images.md](../workflows/images.md).

| Column | Type | Producing step | Notes |
|---|---|---|---|
| `image` | binary (inferred) | `ingest-images` | each source image re-encoded as PNG bytes |
| `label` | inferred (integer class label for the default `timm/oxford-iiit-pet`) | `ingest-images` | carried from the HF dataset |
| `image_id` | inferred | `ingest-images` | the dataset's `image_id`, falling back to the enumeration index |
| `label_cat_dog` | inferred | `ingest-images` | dataset-specific; null when the dataset lacks it |
| `file_size` | `int64` | `lightweight` | `geneva_examples/examples/images/imageinfo.py:build_file_size_udf` |
| `dimensions` | `struct<width: int32, height: int32>` | `lightweight` | `geneva_examples/examples/images/imageinfo.py:build_dimensions_udf` |
| `embedding` | `fixed_size_list<float32, 512>` | `embed` | OpenCLIP ViT-B-32; dim from `geneva_examples/examples/_shared/clip.py:51` |
| `caption_blip` | `string` | `caption` | BLIP; `geneva_examples/examples/_shared/blip.py:57` |

## `videos`

The `videos` table has **three shapes**, one per ingest variant. Each shape pairs
with exactly one chunk step; see [docs/workflows/video.md](../workflows/video.md).

**Shape 1 — inline bytes** (`ingest-videos`,
`geneva_examples/core/utils/videos.py:_to_batch`; pairs with `chunk-videos`):

| Column | Type | Producing step | Notes |
|---|---|---|---|
| `video_id` | `string` | `ingest-videos` | download key |
| `video` | `large_binary` | `ingest-videos` | full MP4 bytes; `--frag-size` controls rows per fragment (see [docs/reference/cli/video.md#ingest-videos](cli/video.md#ingest-videos)) |

**Shape 2 — OpenVid pointer rows** (`ingest-videos-openvid`,
`geneva_examples/core/utils/videos.py:_openvid_target_schema`; pairs with
`chunk-videos-openvid`). No video bytes are ingested; the chunker reads each blob
on the cluster via `take_blobs(ids=[openvid_rowid])`:

| Column | Type | Producing step | Notes |
|---|---|---|---|
| `video_id` | `string` | `ingest-videos-openvid` | source `video_path` |
| `openvid_rowid` | `int64` | `ingest-videos-openvid` | the source dataset's `_rowid` — the stable pointer the chunker reads blobs by |
| `caption` | `string` | `ingest-videos-openvid` | OpenVid metadata, carried through |
| `embedding` | `fixed_size_list<float32, 1024>` | `ingest-videos-openvid` | defensively cast if the scan surfaces it variable-length or float64 |
| `aesthetic_score`, `motion_score`, `temporal_consistency_score` | `float64` | `ingest-videos-openvid` | OpenVid metadata |
| `camera_motion` | `string` | `ingest-videos-openvid` | OpenVid metadata |
| `fps`, `seconds` | `float64` | `ingest-videos-openvid` | OpenVid metadata |
| `frame` | `int64` | `ingest-videos-openvid` | OpenVid frame count — unrelated to `video_clips.frame` |

**Shape 3 — external refs** (`ingest-videos-external`,
`geneva_examples/examples/video/ingest_external_refs.py:190-196`; pairs with
`chunk-videos-external`). Pointer-only: the chunker streams each URI on the
worker:

| Column | Type | Producing step | Notes |
|---|---|---|---|
| `video_id` | `string` | `ingest-videos-external` | object key relative to the listing root, suffix stripped case-insensitively |
| `video_uri` | `string` | `ingest-videos-external` | full `s3://bucket/key` path |
| `size_mb` | `float64` | `ingest-videos-external` | decimal MB, 3 decimal places — the unit `--max-video-mb` compares against |

## `video_clips`

Normally a **materialized view**, created and refreshed by any chunk step — see
[docs/concepts/materialized-views.md](../concepts/materialized-views.md). All
three chunkers emit the same output schema
(`geneva_examples/examples/video/chunkers.py:59-66`,
`geneva_examples/examples/video/chunkers_uri.py:79-87`); the frame stages add the
feature columns.

| Column | Type | Producing step | Notes |
|---|---|---|---|
| `video_id` | `string` | chunk step | inherited automatically: selected in the source projection but not an input column (`inherit_input_columns=False` keeps the source bytes/pointer/URI off clip rows) |
| `chunk_id` | `int32` | chunk step | 0-based window index within the video |
| `start_sec`, `end_sec` | `float32` | chunk step | clip window bounds |
| `clip_bytes` | `large_binary` | chunk step | stream-copy remuxed MP4 (no re-encode) |
| `frame` | `large_binary` | chunk step | JPEG of the start frame, thumbnailed to 512 px longest side, quality 85 |
| `embedding` | `fixed_size_list<float32, dim>` | `frame-embed` | OpenCLIP on `frame`; dimension set by `--dim`, which must match the model (see [docs/reference/cli/video.md#frame-embed](cli/video.md#frame-embed)) |
| `caption` | `string` | `frame-caption` | BLIP on `frame` |
| `pose` | `large_binary` | `frame-openpose` | OpenPose skeleton rendered as PNG; `geneva_examples/examples/video/openpose.py:60` |

Exception: `seed-video-clips` drops the table and recreates it as a **plain
table** (no longer a view) with a skeleton of `video_id` (`string`, random UUID),
`chunk_id` (`int32`), `start_sec`/`end_sec` (`float32`), then backfills `frame`
and (optionally) `clip_bytes` with constant-bytes UDFs
(`geneva_examples/examples/video/seed.py:319-328`).

## `pdfs`

Created by `ingest-pdfs` (`geneva_examples/core/utils/pdfs.py:load_pdf_batches`);
nested columns added by `chunk-pdfs` reusing geneva's shipped document UDFs
(`geneva_examples/examples/pdf/document.py`). Column names are load-bearing:
the shipped UDFs bind their inputs by parameter name — `extract_pages` to
`pdf_bytes`, `chunk_pages` to `pages`. See
[docs/workflows/pdf.md](../workflows/pdf.md).

| Column | Type | Producing step | Notes |
|---|---|---|---|
| `doc_id` | `string` | `ingest-pdfs` | filename stem; duplicates de-duplicated with a `-N` suffix |
| `pdf_bytes` | `large_binary` | `ingest-pdfs` | raw PDF bytes; the exact name `extract_pages` binds to |
| `pages` | `list<struct{page_number: int32, text: large_string}>` | `chunk-pdfs` (first backfill) | one struct per page, `page_number` 0-based; types verified against geneva==0.14.1b5 (`geneva/udfs/document/pdf_embedding.py`) |
| `chunks` | `list<struct{page_number: int32, chunk_id: int32, chunk: large_string}>` | `chunk-pdfs` (second backfill) | `RecursiveCharacterTextSplitter` with `CHUNK_SIZE=2048`, `CHUNK_OVERLAP=200` |

## `audio`

Created by `ingest-audio` (`geneva_examples/examples/audio/ingest.py:53`, an
explicit schema); model columns added by the synthesize and transcribe steps.
`export-audio` adds no column — it writes WAV files to disk. See
[docs/workflows/audio.md](../workflows/audio.md).

| Column | Type | Producing step | Notes |
|---|---|---|---|
| `id` | `string` | `ingest-audio` | prompt id (`greeting`, `pangram`, …); becomes the exported WAV filename |
| `text` | `string` | `ingest-audio` | the prompt to synthesize |
| `audio` | `list<float32>` | `synthesize-audio` | waveform at 16 kHz; the rate is recorded in the column's field metadata as `sample_rate_hz="16000"` (`geneva_examples/examples/audio/tts.py:88`); null for null/blank text |
| `transcript` | `large_string` | `transcribe-audio` | Whisper output; type from geneva's shipped `WhisperChunkTranscriber`, verified against geneva==0.14.1b5 (`geneva/udfs/audio/whisper_transcription.py:168`) |

## `debug_demo`

Created by `demo-errors` (`geneva_examples/examples/debugging/seed_errors.py:80-86`).
See [docs/workflows/debugging-failed-rows.md](../workflows/debugging-failed-rows.md).

| Column | Type | Producing step | Notes |
|---|---|---|---|
| `id` | inferred (`int64`) | `demo-errors` | 1…N |
| `value` | inferred (`int64`) | `demo-errors` | equals `id`, so failures are predictable from the error message |
| `score` | `float64` | `demo-errors` (backfill) | `geneva_examples/examples/debugging/faulty.py:37`; NULL on every row whose UDF call failed under `skip_on_error` |

## System tables: geneva_jobs and geneva_errors

Both are geneva-owned tables in the connection's **system namespace**:
`table_names()` never lists them, so the TUI probes each via
`conn.open_table(name, namespace=list(conn.system_namespace))`
(`geneva_examples/tui/app.py`). `geneva_errors` is append-only — one row per
captured failure, never updated. `geneva_jobs` keeps one row per `job_id` and
updates it in place as the job progresses (`status`, `metrics`, `events`,
`object_ref`, `updated_at`), so it accumulates across runs but preserves no
status history. The canonical name is `geneva_jobs` (the on-disk directory
carries a `___system$` marker prefix, which is an implementation detail).

**geneva_jobs** — one record per backfill/refresh job. Fields verified against
geneva==0.14.1b5 (`geneva/jobs/jobs.py`, `JobRecord`):

| Field | Type | Notes |
|---|---|---|
| `job_id` | string (UUID) | primary key you filter/correlate on |
| `table_name`, `column_name` | string | the job's target |
| `job_type` | string | e.g. `BACKFILL` |
| `status` | string | `PENDING` / `RUNNING` / `DONE` / `FAILED` / `CANCELLED` |
| `launched_at`, `completed_at`, `updated_at` | timestamp | `completed_at` null while active |
| `launched_by` | string | user recorded at launch |
| `config` | string | the launch config as JSON text |
| `object_ref` | string | Ray object ref, when present |
| `manifest_id`, `manifest_checksum` | string | the worker manifest used |
| `metrics` | `list<string>` (each element a JSON object `{name, n, total, done, desc}`) | geneva decodes the JSON to `JobMetric` on read — SQL/Arrow readers see strings; ~35 entries on a backfill; `geneva_examples/core/jobs.py:progress_summary` picks the ratio-shaped ones |
| `events` | list of strings | the append-only event log — the only "log" a job exposes |
| `cluster_name` | string | enterprise cluster identifier |
| `input_columns`, `output_columns` | list of strings | UDF column bindings |

**geneva_errors** — one record per captured UDF failure. Fields verified against
geneva==0.14.1b5 (`geneva/debug/error_store.py`, `ErrorRecord`):

| Field | Type | Notes |
|---|---|---|
| `error_id` | string (UUID) | |
| `error_type` | string | the exception class name, e.g. `ValueError` |
| `error_message` | string | the exception message |
| `error_trace` | string | the full traceback |
| `job_id` | string | scope error reads to one run with it — the store is append-only |
| `table_uri`, `table_name`, `table_version`, `column_name` | string / int | where the failure happened |
| `udf_name`, `udf_version` | string | which UDF build failed |
| `input_columns`, `output_columns` | list of strings | |
| `actor_id`, `fragment_id`, `batch_index` | string / int | execution context |
| `row_address` | int (nullable) | populated for **scalar** UDFs — the hook for retrying only failed rows |
| `attempt`, `max_attempts` | int | retry context |
| `bisect_depth` | int (nullable) | set only for errors isolated via task bisection |
| `timestamp` | timestamp (µs, UTC) | |

## Creation invariants

Every `create_table` call that creates an *example* table passes
`storage_options={"new_table_enable_stable_row_ids": "true"}`
(`OPT_STABLE_ROW_IDS`, `geneva_examples/core/common.py:175`); the UDF Studio
function library (`geneva_examples/apps/studio/library.py:57`) is a plain
LanceDB table and is deliberately exempt. Stable row IDs are
write-time only — there is no migration, only a full rewrite — and any table here
may later become a chunker materialized-view source, so the option goes on
unconditionally. The creating modules are `images/ingest.py`, `video/ingest.py`,
`video/ingest_openvid.py`, `video/ingest_external_refs.py`, `video/seed.py`,
`pdf/ingest.py`, `audio/ingest.py`, and `debugging/seed_errors.py` (all under
`geneva_examples/examples/`); the invariant is pinned on every ingest CLI by
`tests/test_pipeline_ingest_smoke.py`.

In enterprise mode geneva logs `storage_options parameter is not supported when
creating tables on remote connections, ignoring` at table creation — a false
alarm, verified against geneva==0.14.1b5: the options are forwarded and honored
in the client-side Lance write (`geneva_examples/core/common.py:170-174`). Why
the invariant matters — and what breaks without it — is covered in
[docs/concepts/materialized-views.md](../concepts/materialized-views.md).
