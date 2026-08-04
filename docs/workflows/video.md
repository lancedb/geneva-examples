# Video workflow

> Part of the geneva-examples docs — index: [docs/README.md](../README.md).

## Contents

- [The three byte-source variants](#the-three-byte-source-variants)
- [Ingest](#ingest)
- [Chunking into clips](#chunking-into-clips)
- [Detached refresh](#detached-refresh)
- [Frame stages](#frame-stages)
- [Load-testing with seed-video-clips](#load-testing-with-seed-video-clips)
- [Worker credentials for external videos](#worker-credentials-for-external-videos)

## The three byte-source variants

The video example is the largest pipeline in the repo: three ingest variants write a
`videos` table, three paired chunk steps split it into a `video_clips` table of
fixed-length clips (each with a start-frame JPEG), and three frame stages backfill
per-frame features. Per-command flags, types, and defaults are generated from the
step specs: see [docs/reference/cli/video.md](../reference/cli/video.md).

The ingest/chunk steps come in three deliberate variants, one per way of getting
video bytes to a worker (the enumeration lives in the module docstring of
`geneva_examples/examples/video/chunkers_uri.py`):

| Variant | Ingest step | What the `videos` table holds | Paired chunk step | Where the worker gets the bytes |
|---|---|---|---|---|
| Inline bytes | `ingest-videos` | `video_id` + raw `video` bytes (large_binary) | `chunk-videos` | The `video` column is fed to the chunker |
| Lance blob pointer | `ingest-videos-openvid` | OpenVid metadata + `openvid_rowid` pointer, no bytes | `chunk-videos-openvid` | Reads each blob from the OpenVid Lance dataset via `take_blobs` |
| External URI | `ingest-videos-external` | `video_id` + `video_uri` + `size_mb`, no bytes | `chunk-videos-external` | Streams each `video_uri` via `pyarrow.fs.S3FileSystem` |

Each chunk step reads the columns its paired ingest writes (`video`,
`openvid_rowid`, or `video_uri`), so the pairs are not interchangeable: run an
ingest and its paired chunk step together. The runnable pairs, in local mode:

```sh
uv run ingest-videos          && uv run chunk-videos
uv run ingest-videos-openvid  && uv run chunk-videos-openvid
uv run ingest-videos-external --video-bucket <bucket> && uv run chunk-videos-external
```

## Ingest

`ingest-videos` downloads a hardcoded list of MP4s — a single Sintel movie from
archive.org (`VIDEOS` in `geneva_examples/examples/video/ingest.py`) — into a
`videos` table of `video_id` + raw `video` bytes, caching downloads under
`--cache-dir` so re-runs skip the fetch.

`ingest-videos-openvid` registers the first `--limit` rows (scan order) of the
OpenVid Lance dataset (`hf://datasets/lance-format/openvid-lance/data`) as
reference-only rows: the OpenVid metadata columns plus an `openvid_rowid` pointer,
no video bytes — nothing heavy transits the driver
(`geneva_examples/examples/video/ingest_openvid.py`). One asymmetry to know:
unlike every other ingest step in the repo, its `--overwrite` defaults to **off**
(see [docs/reference/cli/video.md#ingest-videos-openvid](../reference/cli/video.md#ingest-videos-openvid)),
so it never drops an existing pointer table on its own. The step only ever calls
`create_table`, so a re-run without `--overwrite` cannot append — it checks for
the table right after connecting and stops there, before reading a single OpenVid
row:

```
table 'videos' already exists — pass --overwrite to drop and re-ingest (the default is off to protect an existing pointer table)
```

Pass `--overwrite` to drop and re-ingest.

`ingest-videos-external` enumerates an S3-compatible bucket and writes pointer rows:
`video_id` (object key relative to the listing root, suffix stripped), `video_uri`
(`s3://bucket/key`), and `size_mb` (`geneva_examples/examples/video/ingest_external_refs.py`).
Selection is controlled by `--limit`, `--smallest-first`, and `--sample stride` — a
systematic sample across the size-sorted listing, so the picks mirror the full size
distribution; any other `--sample` value raises. Bucket credentials come from the
`--video-*` flags, each falling back to the matching `assets_s3_*` key in
`config.yaml` (the assets bucket); the storage bucket `s3_*` credentials are
deliberately never consulted. Missing values fail with
`missing video-bucket credentials (pass --video-* or set assets_s3_* in config.yaml): <missing fields>`
— the message ends with the comma-separated names of the fields that are still blank.

## Chunking into clips

Geneva only runs a chunker inside a materialized view, so each chunk step creates
the view directly under `--clips-table` with `conn.create_udtf_view(...)` and
refreshes it in place — the view *is* the `video_clips` table; there is no `_mv`
sibling and no in-memory copy (`geneva_examples/examples/video/chunk.py`). Filling
the view is a refresh, not a backfill.

All three chunk steps emit the same clip schema: `chunk_id`, `start_sec`,
`end_sec`, `clip_bytes` (a stream-copy remuxed MP4 — no re-encode), and `frame`
(a JPEG of the window's first frame, longest side 512 px). `video_id` is inherited
onto every clip because it is in the source projection but not a chunker input, and
`inherit_input_columns=False` keeps the source bytes/pointer/URI off the clip rows
(`geneva_examples/examples/video/chunkers.py`).

Two constraints:

- **One view = one source + one chunker.** OpenVid clips and movie clips cannot
  share a clips table; give each variant a distinct `--clips-table` name to keep
  both (`geneva_examples/examples/video/chunk_openvid.py`).
- **The source must have stable row IDs.** Every ingest in this repo creates its
  table with them, and each chunk step calls `require_stable_row_ids` before
  creating the view — a source without them would make the view permanently
  unrefreshable, and there is no retrofit. See
  [docs/concepts/materialized-views.md](../concepts/materialized-views.md).

## Detached refresh

`chunk-videos-external --detach` submits the refresh with `view.refresh_async()`
and returns immediately, logging the job id and the sentinel
`chunk_videos_external_submitted` instead of `chunk_videos_external_ok`
(`geneva_examples/examples/video/chunk_external_video.py`). Monitor it with
`uv run jobs tail <job-id>` (see
[docs/workflows/inspecting-state.md](inspecting-state.md)).

`--detach` only detaches in enterprise mode. In local mode it is ignored with a
warning and the refresh runs synchronously: a detached refresh would run inside the
driver-owned local Ray instance, which is torn down when the process exits, killing
the job.

## Frame stages

The frame stages backfill feature columns onto the clips table's `frame` column.
They target `video_clips`; if you chunked into a different `--clips-table` name,
point them there with `--table-name`.

| Step | Column | Re-run behavior |
|---|---|---|
| `frame-embed` | `embedding` | Incremental by default: fills only rows where the column is NULL. `--reset` drops and recomputes everything (needed to switch `--model-name`). |
| `frame-caption` | `caption` | Always drops and recomputes the whole column — no `--reset` flag exists. |
| `frame-openpose` | `pose` | Always drops and recomputes the whole column — no `--reset` flag exists. |

Sources: `geneva_examples/examples/video/frame_{embed,caption,openpose}.py`;
the reset contract lives in `geneva_examples/core/backfill.py` and is explained in
[docs/concepts/backfills.md](../concepts/backfills.md).

**Run-order invariant:** start a frame stage only after the chunk refresh has
completed. Adding (or dropping) the feature column is a schema change, and it breaks
a still-running chunker's schema-matched appends to the same table
(`geneva_examples/core/backfill.py`, `frame_embed.py` docstring). This applies to
both incremental and reset runs.

## Load-testing with seed-video-clips

`uv run seed-video-clips` replicates one decoded clip into N rows that are
byte-identical except for a random `video_id` — a fast fixture for exercising the
frame stages without a full chunk run (`geneva_examples/examples/video/seed.py`).
It works in three phases: decode one clip locally (the only decode), write N tiny
skeleton rows (`video_id`/`chunk_id`/`start_sec`/`end_sec`, ~50 bytes each), then
backfill `frame` and `clip_bytes` with a constant-returning UDF whose payload is
captured in the closure — the bytes ship to the workers once, not per row.

Prerequisites (the step spec's `requires` hint names them — shown in the TUI and in
[docs/reference/cli/video.md#seed-video-clips](../reference/cli/video.md#seed-video-clips),
though `--help` does not print it):

- Without `--seed-clip-table`, it reads its basis video through the
  `openvid_rowid` pointer via `take_blobs`, so it requires a `videos` table
  produced by `ingest-videos-openvid`.
- Alternatively, `--seed-clip-table` reuses a clip already materialized in an
  existing clips table and skips the local decode.

It is also destructive in a specific way: it always drops the clips table (there is
no `--overwrite` flag) and re-creates it as a **plain table**. After seeding,
`video_clips` is no longer a materialized view and cannot be refreshed; re-run a
chunk step to restore the view-backed pipeline.

## Worker credentials for external videos

`chunk-videos-external` resolves the assets bucket credentials on the driver
(explicit `--video-*` flags win, then the `assets_s3_*` block in `config.yaml`),
splits the endpoint into a bare host plus scheme, and delivers them to workers as
five environment variables: `ASSETS_S3_ACCESS_KEY`, `ASSETS_S3_SECRET_KEY`,
`ASSETS_S3_ENDPOINT`, `ASSETS_S3_SCHEME`, `ASSETS_S3_REGION`
(`geneva_examples/examples/video/chunk_external_video.py`). In enterprise mode they
travel in the manifest's `env_vars`; in local mode they are set directly in the
driver process, overwriting any stale ambient values so the UDF sees exactly what
the run was asked to use. The chunker reads them back inside its closure because the
module itself is not importable on workers
(`geneva_examples/examples/video/chunkers_uri.py`).

The full variable table is in
[docs/reference/environment-variables.md](../reference/environment-variables.md);
the pattern and its plaintext caveats are covered in
[docs/authoring/writing-udfs.md](../authoring/writing-udfs.md).
