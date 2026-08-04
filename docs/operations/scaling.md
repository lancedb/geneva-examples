# Scaling

> Part of the geneva-examples docs — index: [docs/README.md](../README.md).

How the fan-out knobs interact, how to run many jobs against a cluster, and where to
look when a job is slow or stuck. Failures with a distinct error message belong in
[docs/operations/troubleshooting.md](troubleshooting.md); the local-mode clamps that
override most of these knobs on a laptop are in
[docs/reference/local-mode.md](../reference/local-mode.md).

## Contents

- [Fan-out knobs](#fan-out-knobs)
- [Running hundreds of jobs](#running-hundreds-of-jobs)
- [Observability on the cluster](#observability-on-the-cluster)
- [Local load-testing](#local-load-testing)

## Fan-out knobs

Parallelism is the product of three decisions: how work is split into tasks, how many
actors may run at once, and how much each actor demands from the cluster. Per-step
defaults are generated — read them from the step's own section in the generated CLI
reference (the [command index](../reference/cli/index.md) maps each command to its
page), not from prose.

| Knob | Passed where | What it bounds |
| --- | --- | --- |
| `task_size` (`--task-size`) | `table.backfill(...)` in `geneva_examples/core/backfill.py` | Rows per read task on a backfill. Passed in both modes on purpose: omitting it lets geneva default to `count_rows() // num_workers // 2`, ignoring the caller (`geneva_examples/core/backfill.py:80-86`). |
| `source_task_size` (`--source-task-size`) | `view.refresh(...)` in the chunk steps (`geneva_examples/examples/video/chunk.py`) | Source rows per chunker expansion task on a refresh. Work items = `ceil(source rows / source_task_size)`. Heavy per-row work (video decode) wants one row per task — see the external chunk step's default in [docs/reference/cli/video.md#chunk-videos-external](../reference/cli/video.md#chunk-videos-external). |
| `concurrency` (`--concurrency`, `--backfill-concurrency`) | `backfill(...)` / `refresh(...)` | Cap on parallel actors. The actor count is `min(work_items, concurrency)` — raising concurrency past the work-item count does nothing, and raising it past what the cluster can host leaves tasks queued. |
| `num_cpus` / `num_gpus` / `memory_gib` (`--num-cpus`, `--num-gpus`, `--memory-gib`) | The UDF/chunker decorator, via `resolve_resources` (`geneva_examples/core/common.py:259-284`) | Per-actor Ray resource demand. Total demand = actors × per-actor demand; this is what drives cluster autoscaling. |
| `worker_max_replicas` | KubeRay cluster configuration (infrastructure, not this repo) | Ceiling on worker pods the autoscaler will add. Concurrency beyond what this ceiling can host shows up as tasks stuck PENDING — raise it for real fan-out. |
| `checkpoint_size` (`--checkpoint-size`, `--backfill-checkpoint-size`) | `backfill(checkpoint_size=…)` remotely; passed as `max_rows_per_fragment` on a refresh (`geneva_examples/examples/video/chunk.py`) | Rows per checkpoint / rows per output fragment. Bounds actor memory and durability granularity; smaller checkpoints survive failures better but add overhead. In local mode the same flag becomes `max_checkpoint_size`, a cap rather than a target (`geneva_examples/core/backfill.py`). |
| `flush_interval_s` (`--flush-interval-s`, `--backfill-flush-interval-s`) | `backfill(batch_checkpoint_flush_interval_seconds=…)` | Max seconds before a partial checkpoint flush — bounds how much work a slow task can lose. |

Which flag family a given step exposes is enumerated in
[docs/operations/troubleshooting.md#tuning-knobs-by-flag-family](troubleshooting.md#tuning-knobs-by-flag-family).
Zero-GPU backfills can additionally be routed to the cluster's CPU-only node groups
with `--use-cpu-only-pool`; this is enterprise-only and never holds GPU work back —
geneva checks the UDF's `num_gpus` first (`geneva_examples/core/backfill.py`).

## Running hundreds of jobs

Every step is a parameterized, re-runnable `run(cfg, *, ...)` function, so a driver
script can loop over inputs and call the functions (or shell out to `uv run <cmd>`)
directly — the TUI is not in the path. Three mechanics make a large batch manageable:

- **Detached refresh.** `uv run chunk-videos-external --detach` submits via
  `view.refresh_async()` and returns immediately with a job id; the refresh runs in a
  Job pod of the deployed Geneva driver on the cluster, not in your local driver
  process. Monitor it with `uv run jobs tail <job-id>`. In local mode `--detach` is
  ignored with a warning (see
  [docs/workflows/video.md#detached-refresh](../workflows/video.md#detached-refresh)).
- **Per-run naming.** `build_manifest` names each worker manifest
  `<prefix>-<6 hex chars>` per run (`geneva_examples/core/common.py:236-248`), and the
  job record stores the manifest name and checksum — so any job in `geneva_jobs` can
  be traced back to the run that submitted it. Job records are the durable surface for
  status, events, and metrics; see
  [docs/workflows/inspecting-state.md](../workflows/inspecting-state.md).
- **No overlap on one table.** A backfill is a schema change and must not run while
  another job appends rows to the same table — batch scripts should serialize steps
  that share a table (for example, frame stages only after the chunk refresh
  finishes). The invariant and its reasoning:
  [docs/concepts/backfills.md](../concepts/backfills.md).

## Observability on the cluster

A detached refresh (or any `refresh_async`/`backfill_async`) runs in a Job pod of
the deployed Geneva driver on the cluster; an attached driver (your machine) only
streams progress bars. The pod and service
names below match LanceDB's standard deployment (`-n lancedb`); adjust for yours.

- **Status and progress, from anywhere with credentials:** capture the returned job
  id, then `uv run jobs show <id>` / `uv run jobs tail <id>` (or `conn.get_job(id)` /
  `conn.list_jobs(...)` — see `geneva_examples/ops/jobs.py`). These read the durable
  `geneva_jobs` system table: status, the append-only event list, and ~35 metrics.
- **Full job and per-task logs:** the deployed Geneva driver's Job pod
  (`kubectl -n lancedb logs <…refresh…-pod> -f`), the worker pods, or the Ray
  dashboard at `raycluster-head-svc:8265` (port-forward it). The `mf` CLI is the
  cluster-operator alternative.
- **Central sinks:** `LANCEDB_OTEL_COLLECTOR_URL` makes geneva push OTLP **metrics
  and trace spans** (one `geneva.job` root span per job) to the cluster's collector,
  so Grafana/Oodle can show job traces and counters — verified against
  geneva==0.14.1b5 (`geneva/telemetry.py`). It does **not** ship logs: driver/worker
  log shipping is a separate cluster-side concern, and without it the pods are the
  only place the logs exist.
- **Stuck PENDING:** almost always capacity — free (GPU) nodes, or the
  `worker_max_replicas` autoscaler ceiling from the table above. Check the Ray
  dashboard's resource demand panel before touching job-side knobs.

## Local load-testing

`uv run seed-video-clips` exists to exercise the frame stages (`frame-embed`,
`frame-caption`, `frame-openpose`) at row counts a full chunk run would take hours to
produce: it decodes one clip locally and replicates it, so seeding 100k rows uploads
a few megabytes of skeleton rows (~50 bytes each) plus one copy of the seed payload,
not the gigabytes a real chunk run would move
(`geneva_examples/examples/video/seed.py`). Its prerequisites, the
`--seed-clip-table` alternative, and the fact that it converts `video_clips` into a
plain table are in
[docs/workflows/video.md#load-testing-with-seed-video-clips](../workflows/video.md#load-testing-with-seed-video-clips);
flags and defaults in
[docs/reference/cli/video.md#seed-video-clips](../reference/cli/video.md#seed-video-clips).
