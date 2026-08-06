# Writing UDFs

> Part of the geneva-examples docs — index: [docs/README.md](../README.md).

How to write the UDFs and chunkers that steps hand to geneva: the factory
pattern, the closure rule that governs what a UDF body may reference, how to get
bytes into a worker, how manifests pin the worker environment, and how to ship
credentials safely. For wiring a UDF into a runnable step, see
[docs/authoring/adding-a-step.md](adding-a-step.md); for reset-vs-incremental
backfill semantics, see [docs/concepts/backfills.md](../concepts/backfills.md).

## Contents

- [The factory pattern](#the-factory-pattern)
- [The closure rule](#the-closure-rule)
- [Choosing a byte source](#choosing-a-byte-source)
- [Manifests and runtime pips](#manifests-and-runtime-pips)
- [Shipping credentials to workers](#shipping-credentials-to-workers)
- [Keeping uploads small](#keeping-uploads-small)
- [Null-safety patterns](#null-safety-patterns)

## The factory pattern

Every UDF and chunker in this repo is produced by a factory function
(`build_clip_embedding_udf`, `chunk_uri_video_udtf`, …) that takes `manifest=`
plus resource kwargs and returns the decorated callable. The factory nests its
heavy imports (`geneva`, `pyarrow`, `torch`) inside its own body so the module
stays import-cheap for the spec registry.

Two conventions every factory follows:

- **A fresh `version=uuid.uuid4().hex` per build.** Geneva caches column-to-UDF
  bindings by version; a fresh version per run means re-running a step
  re-materializes the column with the UDF just built instead of silently reusing
  a stale binding. See `geneva_examples/examples/_shared/clip.py`,
  `geneva_examples/examples/pdf/document.py`,
  `geneva_examples/examples/audio/tts.py`, and
  `geneva_examples/examples/debugging/faulty.py`; pinned by
  `tests/test_udfs.py::test_faulty_score_udf_versions_are_unique` and the pdf
  factory version assertions in the same file.
- **A `memory` request that survives geneva's 32-bit field.** Geneva serializes
  the Ray `memory` request into a signed 32-bit field, so values of `2**31`
  bytes or more raise `OverflowError`. The clamp lives at the step, not in the
  factory: `resolve_resources()` → `memory_request_bytes()`
  (`geneva_examples/core/common.py`) caps any request to `2**31 - 1` with a
  warning, which is why the model factories can keep cluster-sized defaults
  (`_shared/clip.py`, `_shared/blip.py`, and `video/openpose.py` default
  `16 * 1024**3`). Only `video/chunkers_uri.py` hard-defaults to `2**31 - 1`. If
  your factory can be called without passing through `resolve_resources`, cap it
  yourself. Local-mode clamping of all resource requests is tabulated in
  [docs/reference/local-mode.md](../reference/local-mode.md).

An alternative to writing a UDF body: adopt one of geneva's shipped UDFs with
`attrs.evolve(shipped_udf, manifest=..., version=uuid.uuid4().hex, ...)` — the
pattern used by `geneva_examples/examples/pdf/document.py` (page extraction and
chunking) and `geneva_examples/examples/audio/transcribe.py` (Whisper, with
`input_columns` rebound to this repo's column names).

## The closure rule

This is the canonical statement; every UDF module restates a short form of it.

> The decorated function is marshalled **by value** and shipped to workers. The
> module that defines it is **not importable** on the remote runtime — only the
> manifest's pip packages exist there. Therefore every import and every helper
> the function uses must be nested inside the decorated body. A reference to a
> module-level helper or an imported module is marshalled by reference and blows
> up on the worker at deserialization with `ModuleNotFoundError` — the defining
> module is not installed there. A plain module-level constant is worse:
> cloudpickle silently captures its value into the shipped function's globals,
> so the mistake hides until the constant changes. Either way, values the body
> needs from outside must be captured as locals of the factory, so they travel
> inside the closure.

`geneva_examples/examples/video/chunkers_uri.py` shows both halves: the
`ASSETS_S3_*` env-var keys appear as string literals inside the closure (not as
module-level constants that cloudpickle would silently freeze by value), and the
per-worker `fs_cache = {}` dict is a factory local captured by the closure
rather than module state. The self-check when writing a UDF body: could
this function run in a fresh interpreter that has only the manifest's packages
installed and has never imported this repo?

Note the asymmetry with `run()` bodies: step modules run on the driver and may
import freely at call time; only the decorated UDF/chunker body ships to
workers.

## Choosing a byte source

The video example deliberately implements the same chunker three times, once per
byte source, to serve as references
(`geneva_examples/examples/video/chunkers.py` and `chunkers_uri.py`):

| Variant | Factory | Table stores | Worker reads bytes via |
|---|---|---|---|
| Inline bytes | `chunk_video_udtf` | the media itself (`video` large_binary) | the input column, directly |
| Lance blob | `chunk_blob_video_udtf` | a pointer row (`openvid_rowid`) into a blob-encoded Lance dataset | `take_blobs(ids=...)` against the source dataset |
| URI streaming | `chunk_uri_video_udtf` | a URI string (`video_uri`) | `pyarrow.fs.S3FileSystem` opened on the worker, streaming byte ranges |

Recommended default per situation:

- **Small corpora and demos: inline bytes.** Simplest to write and test; the
  table is self-contained. The cost is that the table stores the media.
- **Media already in a Lance dataset with a blob column: Lance blob.** The
  table stays a lightweight pointer and no bytes are duplicated; the worker
  fetches each blob by row id.
- **Media as native files in a separate bucket: URI streaming.** The table
  stays a pure pointer, only a bucket-scoped assets-bucket token (not the
  storage-bucket token) needs read access, and peak worker memory is bounded by
  decode buffers rather than file size — multi-GB files do not OOM an actor.
  This variant requires shipping credentials to the workers (next two sections).

Start with inline bytes; move down the table only when table size, byte
duplication, or credentials force it. All three variants emit the same output
schema, so downstream stages do not change when the byte source does.

## Manifests and runtime pips

A manifest is the pip + env package a remote worker environment is built from.
`build_manifest(cfg, prefix, pip)` in `geneva_examples/core/common.py` is the
mode switch: in local mode it returns `None` (local Ray workers share the
driver's environment, and `@geneva.udf` accepts `manifest=None`); in enterprise
mode it returns
`GenevaManifest.create_pip(f"{prefix}-{uuid4().hex[:6]}").pip(pip).build()` —
the 6-hex suffix makes each manifest name unique per run.

Each UDF module exports a `*_RUNTIME_PIP` list beside its factory (e.g.
`CLIP_RUNTIME_PIP` in `geneva_examples/examples/_shared/clip.py`). The
convention:

- `geneva`, `lancedb`, and `pylance` are resolved through `package_spec()`
  (`geneva_examples/core/package_specs.py`), which reads the **installed**
  version — so workers always match the client's locked environment instead of
  drifting.
- Every other package is exact-pinned in the module, with a
  `{PACKAGE}_PACKAGE_SPEC` environment variable that overrides the spec verbatim
  at module import time.

Do not restate pin values in docs or comments — they drift. The authoritative,
generated inventory of every manifest's specs and every override variable is
[docs/reference/worker-runtime-pins.md](../reference/worker-runtime-pins.md)
(regenerated by `make docs`). When you add a manifest, also add a row to
`tests/test_udfs.py::test_runtime_pip_lists_are_well_formed`, which requires
each list to be non-empty strings including a `geneva==` spec.

## Shipping credentials to workers

The connection's `storage_options` are **not** forwarded to UDFs. If a UDF must
read an external store, the step injects credentials via the manifest's
`env_vars` and the UDF reads them from `os.environ` inside its closure.

The worked example is the external-video pipeline
(`geneva_examples/examples/video/chunk_external_video.py`):

```python
worker_env = {
    "ASSETS_S3_ACCESS_KEY": access_key,
    "ASSETS_S3_SECRET_KEY": secret_key,
    "ASSETS_S3_ENDPOINT": host,     # bare host; scheme carried separately
    "ASSETS_S3_SCHEME": scheme,
    "ASSETS_S3_REGION": region,
}
if cfg.is_local:
    os.environ.update(worker_env)   # local Ray workers share the driver env
    manifest = None
else:
    manifest = (
        GenevaManifest.create_pip(f"chunk-external-{uuid.uuid4().hex[:6]}")
        .pip([*VIDEO_RUNTIME_PIP])
        .env_vars(worker_env)
        .build()
    )
```

Points that matter when replicating this:

- **Local mode uses `os.environ.update`, not `setdefault`** — the values this
  run resolved must beat any stale ambient `ASSETS_S3_*`.
- **The UDF side** (`geneva_examples/examples/video/chunkers_uri.py`) requires
  `ASSETS_S3_ACCESS_KEY` / `ASSETS_S3_SECRET_KEY` / `ASSETS_S3_ENDPOINT` and
  defaults `ASSETS_S3_REGION` to `us-east-1` and `ASSETS_S3_SCHEME` to `https`.
  The full worker-side contract is tabulated in
  [docs/reference/environment-variables.md](../reference/environment-variables.md).
- **Driver-side resolution is explicit**: `--video-*` flags win, then the
  `assets_s3_*` block in `config.yaml`. Ambient `ASSETS_S3_*` never satisfies
  it (`tests/test_video_external.py::test_resolve_video_creds_ignores_ambient_env`)
  — the env vars are the transport the step writes, never an input. The storage
  bucket's `s3_*` credentials are never consulted; the two credential sets do
  not fall back to each other (see
  [docs/getting-started/configuration.md](../getting-started/configuration.md)).

**Security warning — env_vars are plaintext.** Values passed to `env_vars` are
stored in the manifest/job record and shipped to workers as plaintext; anyone
who can read job records can read them. This pattern is acceptable for
bucket-scoped, read-only demo tokens. Production deployments should use a
Kubernetes Secret, a secret store, or workload identity instead of literal
secrets in a manifest.

## Keeping uploads small

Ray rejects any runtime_env `working_dir` upload larger than 512 MiB, and this
repo's working directory (HF caches, `local_db/`, `video_cache/`) easily exceeds
that. Two defenses keep uploads from happening at all:

1. `resolve_config()` in `geneva_examples/core/spec.py` calls
   `os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")`, so unless you
   have exported that variable yourself Ray's `uv run` integration never
   packages the working directory — an ambient
   `RAY_ENABLE_UV_RUN_RUNTIME_ENV=1` wins. Every CLI and the TUI pass through
   this funnel before Ray starts; many `run()` bodies repeat the `setdefault`
   defensively.
2. `.rayignore` at the repo root lists the heavy/generated paths to exclude if
   that integration is ever enabled. It mirrors `.gitignore`'s heavy entries —
   when you add a large local directory, add it to both files.

Relatedly, a large constant payload a UDF needs (e.g. seed bytes) should be
captured once in the closure so it ships once with the marshalled function, not
per row — see the constant-bytes UDF in
`geneva_examples/examples/video/seed.py`.

## Null-safety patterns

Input columns can contain nulls (an upstream step failed a row, or an
incremental backfill left holes). The repo uses four patterns, by output shape:

- **Fixed-size-list output: compute-then-scatter.** A `fixed_size_list` array
  cannot be built from ragged input, so embed only the valid positions and
  scatter the results back into a full-length array with nulls at the invalid
  positions. Reference: the CLIP UDF in
  `geneva_examples/examples/_shared/clip.py`.
- **Scalar/variable output: per-row `None`.** Return `None` for a null or
  undecodable input and let the column hold a null. References: the OpenPose UDF
  (`geneva_examples/examples/video/openpose.py`) returns `None` per undecodable
  image; the MMS-TTS UDF (`geneva_examples/examples/audio/tts.py`) nulls out
  blank text rows.
- **Chunkers: yield nothing.** A chunker that yields no rows for a bad input
  simply produces no output rows; log and continue per window rather than
  raising (see the `encode_failed` handling in
  `geneva_examples/examples/video/chunkers_uri.py`).
- **Deliberate failures: `on_error=skip_on_error()`.** `skip_on_error()` selects
  geneva's SKIP_ROWS fault isolation, which applies rows one at a time and
  records a `row_address` on each error record — the hook for retrying only the
  failed rows — while failing rows are written as NULL and the job completes
  DONE. Scalar and `pa.Array` UDFs get the same per-row isolation; RecordBatch
  UDFs are rejected by SKIP_ROWS. Reference:
  `geneva_examples/examples/debugging/faulty.py`; workflow:
  [docs/workflows/debugging-failed-rows.md](../workflows/debugging-failed-rows.md).
