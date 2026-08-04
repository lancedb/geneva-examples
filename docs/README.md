# geneva-examples documentation

Documentation for geneva-examples: runnable Geneva UDF pipelines for LanceDB,
where one declarative spec per step generates both the `uv run <command>` CLIs
and the interactive TUI. Every page is listed below with a one-line
description. The pages under [reference/cli/](reference/cli/index.md), plus
[reference/worker-runtime-pins.md](reference/worker-runtime-pins.md),
`llms.txt`, and `llms-full.txt` at the repo root, are **generated** — never
edit them; run `make docs` to regenerate.

## Start here

1. [Install](getting-started/install.md) — `uv sync`, the Gemfury indexes,
   what running examples downloads.
2. [Configuration and modes](getting-started/configuration.md) — local vs
   enterprise, every `config.yaml` key, credential sets.
3. Pick a workflow below and run it — local mode needs zero configuration.

## Getting started

- [install.md](getting-started/install.md): prerequisites, `make install`,
  package indexes, caches, verifying the setup
- [configuration.md](getting-started/configuration.md): mode precedence, all
  config keys (including the Azure set), `db_uri` normalization, the R2
  region rule, the storage-vs-assets credential split

## Workflows

- [images.md](workflows/images.md): ingest from Hugging Face → file
  size/dimensions → OpenCLIP embeddings → BLIP captions
- [video.md](workflows/video.md): three ingest variants, three chunk
  variants, three per-frame stages, load-testing with seed-video-clips
- [pdf.md](workflows/pdf.md): ingest your PDFs → per-page text + overlapping
  chunks via Geneva's shipped document UDFs
- [audio.md](workflows/audio.md): the text → speech → text round trip
  (MMS-TTS, Whisper, WAV export) — no media, no cluster, models fetched on
  first run
- [debugging-failed-rows.md](workflows/debugging-failed-rows.md): manufacture
  real backfill failures with demo-errors, then analyze `geneva_errors`
- [tui.md](workflows/tui.md): the interactive runner — keymap, panes,
  retargeting rules, listing limits
- [udf-studio.md](workflows/udf-studio.md): the Gradio prototyping sandbox —
  execution contract, sample data, templates, library, security posture
- [inspecting-state.md](workflows/inspecting-state.md): `stats`, `jobs`
  (list/show/tail/kill), `cleanup`, the job record, and the system tables

## Reference

- [cli/index.md](reference/cli/index.md) *(generated)*: every console script
  in one table
- [cli/images.md](reference/cli/images.md) /
  [cli/video.md](reference/cli/video.md) /
  [cli/pdf.md](reference/cli/pdf.md) /
  [cli/audio.md](reference/cli/audio.md) /
  [cli/debugging.md](reference/cli/debugging.md) *(generated)*: per-command
  flags, types, defaults, and help — identical to `--help`
- [worker-runtime-pins.md](reference/worker-runtime-pins.md) *(generated)*:
  each manifest's pip specs and the `*_PACKAGE_SPEC` override matrix
- [environment-variables.md](reference/environment-variables.md): variables
  you may set, variables the code sets for you, and the worker-side
  `ASSETS_S3_*` contract
- [tables-and-schemas.md](reference/tables-and-schemas.md): every column each
  pipeline produces, plus the `geneva_jobs`/`geneva_errors` system tables
- [local-mode.md](reference/local-mode.md): the clamping table — what every
  resource knob actually becomes on a laptop, and why
- [glossary.md](reference/glossary.md): one definition per term, each
  anchored to a code path

## Concepts

- [architecture.md](concepts/architecture.md): topology in both modes, data
  flow for all five examples, the repository layout
- [spec-and-cli-generation.md](concepts/spec-and-cli-generation.md): how
  Example → Step → Param becomes console scripts and TUI forms
- [backfills.md](concepts/backfills.md): reset vs incremental — the
  destructive default, the UDF-rebinding rule, the overlap invariant
- [materialized-views.md](concepts/materialized-views.md): the clips table as
  a materialized view, stable row IDs, and the unrecoverable-view failure this
  repo guards against

## Authoring

- [adding-a-step.md](authoring/adding-a-step.md): the single checklist for
  adding a step or a whole example, including the test and docs steps
- [writing-udfs.md](authoring/writing-udfs.md): the factory pattern, the
  closure rule, byte-source choices, manifests, worker credentials
- [testing.md](authoring/testing.md): the two-tier geneva mock, the
  smoke-test recipe, coverage-gate mechanics, test hygiene

## Operations

- [troubleshooting.md](operations/troubleshooting.md): symptom → cause → fix,
  plus how to get more logs and which tuning knob family each step has
- [scaling.md](operations/scaling.md): fan-out knobs, running hundreds of
  jobs, observing them on the cluster
- [version-pins.md](operations/version-pins.md): the two pin tiers, the
  cluster upgrade runbook, and the pin-fragility inventory

## Conventions used in these docs

| Term | Meaning | Never called |
| --- | --- | --- |
| example / step / param | The spec objects in `geneva_examples/core/spec.py`; an example owns ordered steps, a step owns params | task, stage, pipeline object |
| backfill | Filling a column of an existing table with UDF output | refresh |
| refresh | Recomputing a materialized view from its source table | backfill |
| materialized view | The table a chunker produces via `create_udtf_view` | MV, UDTF view (glossary lists synonyms once) |
| storage bucket | The `s3_*` credential set — the connection's `storage_options` for the LanceDB data files | assets bucket |
| assets bucket | The `assets_s3_*` credential set — the raw-media bucket the external-refs video steps read | storage bucket |
| driver | The client process where the CLIs run; the cluster-side Geneva process is always called the *deployed Geneva driver*, never just "driver" | client, submitter |
| worker | A remote Ray actor executing UDF closures | node, executor |
| local mode / enterprise mode | On-disk Lance DB + local Ray vs LanceDB Enterprise + remote Geneva runtime | dev/prod |
| manifest | The pip + env-var package a worker installs (enterprise only) | runtime env |

## Finding things (grep hints)

Agents: fetch `llms.txt` (repo root) for the curated index, or `llms-full.txt`
for the whole corpus in one file. Useful literal searches:

```bash
grep -n '^## `chunk-videos`' docs/reference/cli/video.md   # any command's flag table
grep -rn "ASSETS_S3_" docs/reference/environment-variables.md
grep -rn "us-east-1" docs/                                 # the R2 region rule
grep -n "max_checkpoint_size" docs/reference/local-mode.md # local knob renames
grep -rn "reset=True" docs/concepts/backfills.md           # destructive vs incremental
grep -rn "stable row" docs/concepts/materialized-views.md  # the view invariant
grep -rn "OPT_STABLE_ROW_IDS" geneva_examples/ tests/      # where code enforces it
grep -rn "geneva_errors" docs/workflows/debugging-failed-rows.md
```

## Where the source of truth is

| Doc area | Authoritative code |
| --- | --- |
| Spec model + CLI generation | `geneva_examples/core/spec.py` |
| Example/step registry | `geneva_examples/examples/__init__.py` |
| Config keys + mode resolution | `geneva_examples/core/config.py` |
| Backfill contract (reset/incremental) | `geneva_examples/core/backfill.py` |
| Connection, manifests, local clamping | `geneva_examples/core/common.py` |
| Job records and rendering | `geneva_examples/core/jobs.py` |
| Version pins + tool config | `pyproject.toml` |
| Docs generator | `geneva_examples/docs_gen/` |
