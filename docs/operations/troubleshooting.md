# Troubleshooting

> Part of the geneva-examples docs — index: [docs/README.md](../README.md).

One row per observed symptom. Every cause cites the code path that produces or guards
the behavior; fixes link to the page that owns the full story. For throughput problems
(slow but not failing), see [docs/operations/scaling.md](scaling.md).

## Contents

- [Symptom → cause → fix](#symptom--cause--fix)
- [Getting more logs](#getting-more-logs)
- [Tuning knobs by flag family](#tuning-knobs-by-flag-family)

## Symptom → cause → fix

| Symptom | Cause | Fix |
| --- | --- | --- |
| `config file not found` / `missing required config: …` | Enterprise mode requires `config.yaml` with `lancedb_api_key`, `lancedb_region`, `geneva_host` (`geneva_examples/core/config.py`). The file resolves relative to the working directory, so running from a subdirectory can also mean "no config found". | Copy `config-example-enterprise.yaml` to `config.yaml` and fill the three keys, or run in local mode. Run commands from the repo root. See [docs/getting-started/configuration.md](../getting-started/configuration.md). |
| `uv sync` cannot resolve a `geneva==…bN` / `lancedb==…bN` / `pylance==…bN` pin | The pinned betas exist only on the two explicit Gemfury indexes declared in `pyproject.toml` (`[[tool.uv.index]]`); public PyPI carries geneva final releases only. | Keep the index blocks intact and confirm network access to `pypi.fury.io`. See [docs/getting-started/install.md](../getting-started/install.md). |
| Worker pip install fails `ResolutionImpossible`, listing two conflicting `geneva==` pins | Version skew between this repo and the deployed Geneva driver: geneva's Ray manager prepends its own `geneva==` pin to the runtime pip list while this repo's manifests add the installed driver version (comment block in `pyproject.toml`). | Re-pin this repo to the deployed Geneva driver's exact versions: [docs/operations/version-pins.md](version-pins.md). |
| `declare_table` or backfill requests fail with HTTP 500 | `lancedb`/`pylance` skew between this repo and the deployed Geneva driver (comment block in `pyproject.toml`). | Same runbook: [docs/operations/version-pins.md](version-pins.md). |
| HTTP 403 `SignatureDoesNotMatch` from object storage on Cloudflare R2 | R2 treats `us-east-1` as the SigV4 alias for its `auto` region; other region values (such as `enam`) are rejected. | Set `lancedb_region` and `s3_region` to `us-east-1` in `config.yaml`. See [docs/getting-started/configuration.md](../getting-started/configuration.md). |
| Stray `./<name>/` directory at the repo root containing `__manifest/` and a `…___system$geneva_jobs/` subdirectory | A scheme-less `db_uri` (for example `smoke`): geneva reads any non-`db://` URI as an on-disk database created relative to the working directory. `normalize_db_uri` prepends `db://` and logs a WARNING — in enterprise mode only; local mode leaves `db_uri` untouched because it is unused there (`geneva_examples/core/config.py:159-160`). | Delete the stray directory (`.gitignore` masks them via the `/*/__manifest/` marker globs) and use a full `db://<name>` URI. |
| `storage_options parameter is not supported when creating tables on remote connections, ignoring` logged during an enterprise ingest | False alarm: geneva forwards the options anyway and the client-side Lance write honours them, so stable row IDs are applied. Verified against geneva==0.14.1b5 (`geneva_examples/core/common.py:159-175`). | No action needed. Background: [docs/concepts/materialized-views.md](../concepts/materialized-views.md). Re-verify on any geneva pin bump: [docs/operations/version-pins.md](version-pins.md). |
| Ray fails packaging or uploading a `working_dir` (512 MiB limit) | Ray's `uv run` runtime-env integration packaged the whole working directory (HF caches, `local_db/`, …). The repo defends twice: `resolve_config` sets `RAY_ENABLE_UV_RUN_RUNTIME_ENV=0` (`geneva_examples/core/spec.py`) and `.rayignore` excludes heavy paths. | Do not override that variable; add any new heavy directory to `.rayignore`. See [docs/reference/environment-variables.md](../reference/environment-variables.md). |
| `RuntimeError: no PDFs loaded from ./studio_data/pdfs` | The default PDF directory is tracked empty (only `.gitkeep`); the repo bundles no sample PDFs (`geneva_examples/examples/pdf/ingest.py:45`). | Copy your own PDFs into `studio_data/pdfs/`, or pass `--pdf-dir`. See [docs/workflows/pdf.md](../workflows/pdf.md). |
| `RuntimeError: required columns not visible: […]` | `add_columns` had not propagated to a reopened table within the wait window (`geneva_examples/core/utils/tables.py:25`). | Raise `--schema-wait-attempts` / `--schema-wait-sleep-s` on the step (per-step defaults: the step's section in the generated CLI reference, e.g. [`embed`](../reference/cli/images.md#embed); the [command index](../reference/cli/index.md) maps every command to its page). |
| A feature column stays NULL after a step returns | Rows were skipped — unreadable input, or the UDF raised per row. `table.backfill()` is synchronous, so the step only returns once the job is terminal; the `null_<column> <count>` it logs is therefore final (`geneva_examples/core/backfill.py:120-131`). | Inspect per-row failures via `geneva_errors`: [docs/workflows/debugging-failed-rows.md](../workflows/debugging-failed-rows.md); the job record itself with `uv run jobs` (add `--all` for terminal states). |
| Backfill aborted as stalled by the watchdog, with no rows in `geneva_errors` | No task completed inside geneva's stall window, so the pipeline aborts with `TimeoutError: Pipeline stalled: no task completed in <N>s` — usually a read task too large for a slow UDF, not a hang (`geneva_examples/core/backfill.py:80-86`). | Lower `--task-size` so tasks complete — and so reset the stall deadline — more often. Knob interplay: [docs/operations/scaling.md](scaling.md). |
| Local run hangs / a task never schedules | Not a CPU/GPU over-request: local mode forces `num_gpus=0` and caps `num_cpus` to the core count unconditionally for every step (`geneva_examples/core/common.py:259-284`), and `_admission_check=False` makes tasks queue rather than be rejected (`geneva_examples/core/backfill.py:108-111`). Look instead at total demand — `--concurrency` × per-actor `--memory-gib` — or at a UDF that is simply slow. | Lower `--concurrency` / `--memory-gib`, or `--task-size` so tasks finish sooner. The full clamping table: [docs/reference/local-mode.md](../reference/local-mode.md). |
| A materialized view refresh fails after the source table's version moved (for example after compaction) | The source lacks stable row IDs: the view pinned `geneva::view::base_table_version` and can never advance it. The maintenance agent compacts tables past 30 uncompacted fragments on its own, so no user action is needed to trigger this (`geneva_examples/core/common.py:178-205`). | Unrecoverable for that view — drop and re-ingest the source (this repo's ingest steps always enable stable row IDs). See [docs/concepts/materialized-views.md](../concepts/materialized-views.md). |
| `missing video-bucket credentials (pass --video-* or set assets_s3_* in config.yaml)` | The external-refs video steps read the assets bucket; the storage bucket credentials (`s3_*`) are deliberately never consulted for it (`geneva_examples/examples/video/ingest_external_refs.py:80`). | Set the `assets_s3_*` block in `config.yaml` or pass the `--video-*` flags. See [docs/getting-started/configuration.md](../getting-started/configuration.md). |
| Jobs stuck PENDING, or running far below the requested concurrency | The cluster has no free (GPU) capacity, or the autoscaler hit its worker-pod ceiling (`worker_max_replicas`). | Inspect with `uv run jobs`, then follow the cluster-side checks in [docs/operations/scaling.md#observability-on-the-cluster](scaling.md#observability-on-the-cluster). |
| Hugging Face rate limits during ingest | Anonymous downloads are throttled. | Set `hf_token` in `config.yaml` (see [docs/getting-started/configuration.md](../getting-started/configuration.md)). |

## Getting more logs

Every command accepts `--log-level` (one of the four common options generated for each
step — see any per-example page's "Common options" table, e.g.
[docs/reference/cli/images.md](../reference/cli/images.md#common-options-every-command)).
At the default
level, `setup_logging` quiets the `ray`, `lancedb`, `pylance`, and `geneva` loggers to
WARNING, sets `LANCE_LOG=warn` to silence lance's Rust event stream at the source, and
local runs disable Ray worker-log forwarding for a clean console
(`geneva_examples/core/common.py:42-65`, `geneva_examples/core/common.py:208-233`).

Pass `--log-level DEBUG` to restore everything:

- the noisy loggers stay at DEBUG instead of being capped at WARNING;
- `LANCE_LOG` is not forced to `warn`, so lance's per-fragment event logs return;
- in local mode, `runtime_session` re-enables Ray worker-log forwarding to the
  process that launched the run, so UDF-side prints and errors stream back.

`LANCE_LOG` is applied with `setdefault`, so a value you exported beforehand always
wins. It must be set before lance is imported; workers inherit it from the *driver's*
environment — which is the launching process only in local mode, not on a remote
cluster. See
[docs/reference/environment-variables.md](../reference/environment-variables.md).

## Tuning knobs by flag family

Three flag families exist, and no step carries more than one of them. Per-step
defaults are machine-generated — read them from the generated CLI reference, never
from prose.

| Step class | Steps | Throughput flags |
| --- | --- | --- |
| Lightweight CPU backfills | [`lightweight`](../reference/cli/images.md#lightweight), [`chunk-pdfs`](../reference/cli/pdf.md#chunk-pdfs) | `--backfill-concurrency`, `--backfill-task-size`, `--backfill-checkpoint-size`, `--backfill-flush-interval-s`, `--backfill-timeout-min`, `--use-cpu-only-pool` |
| Standard backfill stages (model stages, seeder, demo) | [`embed`](../reference/cli/images.md#embed), [`caption`](../reference/cli/images.md#caption), [`frame-embed`](../reference/cli/video.md#frame-embed), [`frame-caption`](../reference/cli/video.md#frame-caption), [`frame-openpose`](../reference/cli/video.md#frame-openpose), [`synthesize-audio`](../reference/cli/audio.md#synthesize-audio), [`transcribe-audio`](../reference/cli/audio.md#transcribe-audio), [`seed-video-clips`](../reference/cli/video.md#seed-video-clips), [`demo-errors`](../reference/cli/debugging.md#demo-errors) | `--concurrency`, `--task-size`, `--checkpoint-size`, `--flush-interval-s`, `--backfill-timeout-min` (plus `--batch-size` / `--num-workers` / `--num-cpus` / `--num-gpus` / `--memory-gib` on the model stages) |
| Materialized-view refreshes | [`chunk-videos`](../reference/cli/video.md#chunk-videos), [`chunk-videos-openvid`](../reference/cli/video.md#chunk-videos-openvid), [`chunk-videos-external`](../reference/cli/video.md#chunk-videos-external) | `--concurrency`, `--checkpoint-size` (max rows per output fragment), `--source-task-size` — there is no `--task-size` on a refresh |

Two corrections to older docs worth restating: the `--backfill-concurrency` /
`--backfill-task-size` / `--backfill-checkpoint-size` / `--backfill-flush-interval-s`
group exists only on the two lightweight steps above, not on every stage —
`--backfill-timeout-min` is the one prefixed flag every backfilling step carries; and
the model stages' remaining throughput flags are unprefixed. A flag's meaning also
shifts in local mode — `--checkpoint-size`
becomes a cap rather than a target, concurrency is capped to the core count, and GPU
requests are zeroed. See [docs/reference/local-mode.md](../reference/local-mode.md)
for the exact clamps and [docs/operations/scaling.md](scaling.md) for what each knob
bounds on a real cluster.
