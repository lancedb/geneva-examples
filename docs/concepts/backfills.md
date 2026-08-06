# Backfills: reset vs incremental

> Part of the geneva-examples docs — index: [docs/README.md](../README.md).

## Contents

- [What a backfill is](#what-a-backfill-is)
- [reset=True: drop and recompute](#resettrue-drop-and-recompute)
- [reset=False: incremental](#resetfalse-incremental)
- [Which steps expose --reset](#which-steps-expose---reset)
- [The overlap invariant](#the-overlap-invariant)
- [Local vs remote knobs](#local-vs-remote-knobs)
- [Observable log lines](#observable-log-lines)

## What a backfill is

A backfill fills one UDF-backed column of an existing table. Every feature step in
this repo funnels through a single function, `backfill_column()` — see
`geneva_examples/core/backfill.py` for the authoritative contract. Its flow: check
whether the column exists → (on reset) drop it → `add_columns({column: udf})` → wait
for the column to become visible → `table.backfill(...)` → `checkout_latest()` and log
the remaining NULL count.

In these docs "backfill" always means filling a column. Filling a materialized view
from its source is a "refresh" — a separate flow covered in
[materialized-views.md](materialized-views.md).

## reset=True: drop and recompute

`reset=True` is the default of `backfill_column`
(`geneva_examples/core/backfill.py:37`). When the column already exists it is
dropped, `add_columns` re-binds it to the current UDF, and the backfill recomputes
every row. This is destructive — it wipes the prior values — but it guarantees the
whole column reflects the current UDF/model. Dropping and re-adding the column is a
schema change, so it must not run concurrently with another job appending rows to the
same table (see [The overlap invariant](#the-overlap-invariant)).

A drop that fails is tolerated (the column may have vanished between the existence
check and the drop); any real problem then surfaces from `add_columns`
(`geneva_examples/core/backfill.py:59-68`).

## reset=False: incremental

`reset=False` keeps the existing column and fills only the rows still missing it —
`table.backfill` defaults its filter to `<column> IS NULL`. It is safe to run
repeatedly: each pass picks up whatever rows landed since the last one. On the first
run (column absent) both modes behave identically
(`geneva_examples/core/backfill.py:48-56`).

The catch: an incremental run keeps the column's originally-registered UDF. Both
modes rely on the UDF binding that `add_columns` set, never a `backfill(udf=...)`
override — that override is unsupported on remote/enterprise connections
(`backfill_async` raises `NotImplementedError`; you would have to `alter_columns()`
first). So an incremental re-run on a pre-existing column fills the NULL rows with
the original model's output even if you invoked the step with a different
`--model-name`. To swap the UDF or model, pass `--reset` so the column is dropped,
re-bound, and fully recomputed (`geneva_examples/core/backfill.py:113-118`).

## Which steps expose --reset

Only three steps expose the choice, and all three default to incremental
(`reset: bool = False` in their `run()` signatures). Every other feature step calls
`backfill_column` without a `reset` argument and therefore destructively recomputes
its column(s) on every run.

| Command | Default table | Column(s) | Re-run behavior | Source |
| --- | --- | --- | --- | --- |
| `frame-embed` | `video_clips` | `embedding` | incremental; `--reset` rebuilds | `geneva_examples/examples/video/frame_embed.py:42` |
| `synthesize-audio` | `audio` | `audio` | incremental; `--reset` rebuilds | `geneva_examples/examples/audio/synthesize.py:43` |
| `transcribe-audio` | `audio` | `transcript` | incremental; `--reset` rebuilds | `geneva_examples/examples/audio/transcribe.py:110` |
| `lightweight` | `images` | `file_size`, `dimensions` | destructive recompute every run | `geneva_examples/examples/images/lightweight.py` |
| `embed` | `images` | `embedding` | destructive recompute every run | `geneva_examples/examples/images/embed.py` |
| `caption` | `images` | `caption_blip` | destructive recompute every run | `geneva_examples/examples/images/caption.py` |
| `frame-caption` | `video_clips` | `caption` | destructive recompute every run | `geneva_examples/examples/video/frame_caption.py` |
| `frame-openpose` | `video_clips` | `pose` | destructive recompute every run | `geneva_examples/examples/video/frame_openpose.py` |
| `seed-video-clips` | `video_clips` (seeded plain table) | `frame`, `clip_bytes` | drops and re-creates the whole clips table, then backfills both columns (`clip_bytes` unless `--no-include-clip-bytes`) | `geneva_examples/examples/video/seed.py` |
| `chunk-pdfs` | `pdfs` | `pages`, `chunks` | destructive recompute every run | `geneva_examples/examples/pdf/chunk.py` |
| `demo-errors` | `debug_demo` | `score` | destructive recompute every run (deliberate: the demo re-seeds errors) | `geneva_examples/examples/debugging/seed_errors.py` |

Do not generalize from the three incremental steps: a second `uv run embed` recomputes
the whole `embedding` column on `images`, weights download and all. `chunk-pdfs`,
despite its name, is a backfill of two list columns — not a materialized-view refresh.

Per-command flags and defaults live in the generated reference:
[docs/reference/cli/video.md#frame-embed](../reference/cli/video.md#frame-embed),
[docs/reference/cli/audio.md#synthesize-audio](../reference/cli/audio.md#synthesize-audio),
[docs/reference/cli/audio.md#transcribe-audio](../reference/cli/audio.md#transcribe-audio),
and [docs/reference/cli/index.md](../reference/cli/index.md) for everything else.

## The overlap invariant

Neither mode may run while another job is appending rows to the same table. Adding
the column — which both modes do when it is absent, and which `reset=True` forces by
dropping first — is a schema change, and a still-running producer performs
schema-matched appends that the change breaks
(`geneva_examples/core/backfill.py:43-56`).

Concretely: run `frame-embed`, `frame-caption`, or `frame-openpose` only after the
chunk refresh that populates `video_clips` has completed
(`geneva_examples/examples/video/frame_embed.py:44-54`). For a detached refresh
(`chunk-videos-external --detach`), wait until `uv run jobs tail <job-id>` reports the
job DONE before starting a frame step — see
[docs/workflows/video.md](../workflows/video.md).

## Local vs remote knobs

`backfill_column` branches on `conn.is_remote()` and passes different kwargs to
`table.backfill` (`geneva_examples/core/backfill.py:86-111`). The CLI flags are the
same in both modes; what they mean is not.

| Knob | Enterprise mode (remote) | Local mode |
| --- | --- | --- |
| `task_size` | passed as-is | passed as-is |
| `checkpoint_size` | passed as `checkpoint_size` — a target | passed as `max_checkpoint_size` — a cap |
| `use_cpu_only_pool` | passed through | never passed |
| `concurrency` | passed as requested | re-capped by `local_concurrency()` to cpu_count − 1, floor 1 |
| `_admission_check` | not passed | `False` — tasks queue for a free slot instead of the job being rejected up front |

- `task_size` is passed in both modes: it is plain read-task planning (rows per
  worker task), and geneva honours it either way — the local path forwards it to
  `dispatch_run_ray_add_column`, the remote path into the namespace
  `AlterTableBackfillColumnsRequest` (verified against geneva==0.14.1b5,
  `geneva/table.py:3276-3300`, `geneva/table.py:4988-5005`). Omitting it would let
  geneva default to
  `count_rows() // num_workers // 2`, ignoring the caller's setting. It matters most
  in local mode, where an oversized task on a slow UDF is what trips the stall
  watchdog (`geneva_examples/core/backfill.py:80-86`).
- `use_cpu_only_pool` keeps appliers that request no GPU off the GPU nodes, via a
  custom Ray resource that only the cluster's CPU-only node groups advertise. It
  never holds GPU work back: geneva branches on the UDF's `num_gpus` first and
  consults this flag only when that is zero, so a GPU UDF is scheduled on GPUs either
  way. It is remote-only because local Ray advertises no such resource — requesting
  it would leave the tasks unschedulable (`geneva_examples/core/backfill.py:92-99`).
- `local_concurrency()` caps concurrency to cpu_count − 1 (leaving a core for the
  raylet/driver) with a floor of 1 (`geneva_examples/core/common.py:292-299`). Model
  steps typically also hard-set concurrency to 1 in local mode via `local_or` before
  this cap applies. The full local clamp table is in
  [docs/reference/local-mode.md](../reference/local-mode.md).

## Observable log lines

`backfill_column` emits stable, greppable lines per column
(`geneva_examples/core/backfill.py:127-131`):

| Line | Meaning |
| --- | --- |
| `job <column> <job_id>` | the geneva job id — feed it to `uv run jobs show <job-id>` or `uv run jobs tail <job-id>` |
| `backfill_seconds <column> <seconds>` | wall-clock duration of the `table.backfill` call |
| `null_<column> <count>` | NULL rows remaining after `checkout_latest()` |

A non-zero `null_<column>` after a run means rows are still unfilled: either per-row
UDF failures skipped by a `skip_on_error` UDF (the debugging example demonstrates
this — see
[docs/workflows/debugging-failed-rows.md](../workflows/debugging-failed-rows.md)), or
rows appended after the backfill planned its tasks. Where a step supports it, an
incremental re-run fills exactly those rows.
