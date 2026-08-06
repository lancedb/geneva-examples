# Local-mode behavior: how every resource knob is clamped

> Part of the geneva-examples docs — index: [docs/README.md](../README.md).

## Contents

- [What local mode is](#what-local-mode-is)
- [The clamping table](#the-clamping-table)
- [Ray provisioning](#ray-provisioning)
- [Where the numbers are pinned](#where-the-numbers-are-pinned)

## What local mode is

Local mode connects to an on-disk Lance database at `local_db_path` (default
`./local_db`) and runs every backfill and refresh on a local Ray instance
provisioned for the duration of the run — no cluster, no GPU, no secrets, and no
`config.yaml` required. See `geneva_examples/core/common.py` (`connect`,
`runtime_session`) for the authoritative contract, and
[docs/getting-started/configuration.md](../getting-started/configuration.md) for
how a run resolves to local mode in the first place.

Two consequences worth knowing before tuning anything:

- `--db-uri` is ignored in local mode; the only connection target is
  `local_db_path` (`geneva_examples/core/common.py:142-148`).
- `chunk-videos-external --detach` is ignored with a warning and the refresh runs
  synchronously
  ([docs/workflows/video.md#detached-refresh](../workflows/video.md#detached-refresh)).

Because a laptop has no GPU pool and only a handful of cores, the cloud-tuned
defaults on every step would either never schedule or oversubscribe the machine.
Instead of maintaining a second set of defaults, the code clamps the values you
pass. The table below is the complete list of those clamps.

## The clamping table

| Flag / knob | You pass | Local mode uses | Mechanism (file) |
|---|---|---|---|
| `--num-gpus` | any value | `0`, unconditionally | `resolve_resources` — `geneva_examples/core/common.py:284` |
| `--num-cpus` | `N` | `max(1, min(int(N), os.cpu_count()))` as a float; a fractional request below 1 (e.g. `0.5`) truncates to `0` and is then floored to `1.0` | `resolve_resources` — `geneva_examples/core/common.py:280` |
| `--memory-gib` | `G` | `min(G·2³⁰, 2³¹−1 bytes, max(256 MiB, 25% of RAM))` | `memory_request_bytes` + the local RAM cap in `resolve_resources` — `geneva_examples/core/common.py:118-129,281-283` |
| `--batch-size` | any value | `8` (model steps) | `local_or(cfg, 8, batch_size)` in each model step, e.g. `geneva_examples/examples/images/embed.py:57` |
| `--num-workers` | any value | `0` DataLoader workers (model steps) | `local_or(cfg, 0, num_workers)`, e.g. `geneva_examples/examples/images/embed.py:58` |
| `--concurrency` | any value | `1` for model steps via `local_or(cfg, 1, concurrency)`; then `backfill_column` and the chunk steps cap *any* concurrency to `cpu_count − 1` (floor 1) via `local_concurrency` | `geneva_examples/core/common.py:292-299`, `geneva_examples/core/backfill.py:107`, `geneva_examples/examples/video/chunk.py:122` |
| `--checkpoint-size` | `N` | passed as `max_checkpoint_size=N` — the adaptive upper bound (it also caps the largest read batch, so it bounds per-batch memory); remotely the same flag is `checkpoint_size`, which seeds the *initial* adaptive checkpoint size. Both are maxima, not targets | `geneva_examples/core/backfill.py:100-111` |
| (not a flag) `_admission_check` | — | `False`: tasks queue for a free slot instead of the job being rejected by admission pre-flight | `geneva_examples/core/backfill.py:108-111`, `geneva_examples/examples/video/chunk.py:123` |
| `--use-cpu-only-pool` | true/false | ignored — it is only sent on remote backfills, because it requests a custom Ray resource that only the cluster's CPU-only node groups advertise | `geneva_examples/core/backfill.py:88-99` |
| `--task-size` | `N` | honored as-is, in **both** modes | `geneva_examples/core/backfill.py:80-87` |

Notes on individual rows:

- **Memory.** The `2³¹−1` cap (≈2 GiB) applies in *both* modes: geneva serializes
  the Ray `memory` request into a signed 32-bit field on the namespace API, and a
  value of `2³¹` or more raises `OverflowError`. Capping is safe because `memory`
  is only an advisory Ray scheduling reservation
  (`geneva_examples/core/common.py:32-35`). The 25%-of-RAM cap is local-only and
  is skipped entirely when total RAM cannot be read.
- **Which steps use `local_or`.** The image and video-frame model steps clamp all
  three of batch size / workers / concurrency; the audio model steps
  (`synthesize-audio`, `transcribe-audio`) clamp only concurrency
  (`geneva_examples/examples/audio/synthesize.py:65`).
- **`task_size` on purpose.** It is plain read-task planning (rows per worker
  task) and geneva routes local and remote backfills through the same code path.
  Omitting it would let geneva default to `count_rows() // num_workers // 2`,
  ignoring your flag — and locally, an oversized task on a slow UDF is exactly
  what trips the stall watchdog (`geneva_examples/core/backfill.py:80-87`).
- **Per-step defaults are not restated here.** Every flag's default lives in the
  generated reference — see [docs/reference/cli/index.md](cli/index.md).

## Ray provisioning

`runtime_session` (`geneva_examples/core/common.py:208-233`) provisions one local
Ray instance for the whole run and tears it down on exit, which is why it wraps a
step's entire backfill loop rather than each column. It calls geneva's **private**
API `geneva.runners.ray._mgr.ray_cluster(local=True, log_to_driver=False,
logging_level=WARNING)` for a quiet console (`--log-level DEBUG` turns worker-log
forwarding back on).

Known fragility: on *any* exception importing or calling that private API, the
code silently falls back to the public `conn.local_ray_context()`, which
hardcodes `log_to_driver=True` and is much noisier. There is no log line marking
the fallback, so a geneva pin bump that moves the private API degrades every
local run without a signal. The fallback itself is pinned by
`tests/test_core.py::test_runtime_session_falls_back_to_public_api`; see
[docs/operations/version-pins.md](../operations/version-pins.md) for the other
private-API dependencies.

## Where the numbers are pinned

Every number in the clamping table is asserted by a test, so a claim on this page
can be verified by running the named test:

| Claim | Pinning test |
|---|---|
| `num_gpus` → 0, `num_cpus` capped to core count | `tests/test_core.py::test_resolve_resources_clamps_locally` |
| memory capped to 25% of RAM on a small box | `tests/test_core.py::test_resolve_resources_caps_memory_on_small_box` |
| 256 MiB memory floor | `tests/test_core.py::test_resolve_resources_memory_floor` |
| enterprise passes requests through unchanged | `tests/test_core.py::test_resolve_resources_passthrough_enterprise` |
| RAM unreadable → RAM cap skipped | `tests/test_core.py::test_resolve_resources_local_without_ram_reading` |
| 2³¹−1 memory cap with a warning | `tests/test_core.py::test_memory_request_bytes_caps_to_32bit` |
| `local_or` picks the local value only in local mode | `tests/test_core.py::test_local_or_picks_by_mode` |
| concurrency capped to `cpu_count − 1`, floor 1 | `tests/test_core.py::test_local_concurrency_caps_leaving_a_core`, `tests/test_core.py::test_local_concurrency_floor` |
| local backfill sends `max_checkpoint_size` + `_admission_check=False` | `tests/test_pipeline_runner.py::test_backfill_column_local_uses_native_kwargs` |
| remote backfill sends `checkpoint_size` + `use_cpu_only_pool` | `tests/test_pipeline_runner.py::test_backfill_column_happy_path_enterprise` |
| quiet Ray provisioning + DEBUG verbosity | `tests/test_core.py::test_runtime_session_local_disables_log_forwarding`, `tests/test_core.py::test_runtime_session_local_verbose_in_debug` |
