# Adding a step

> Part of the geneva-examples docs — index: [docs/README.md](../README.md).

This page is the single authoritative procedure for adding a step (one
`uv run <name>` command) to an existing example, or adding a whole new example.
It supersedes the overlapping walkthroughs that used to live in `AUTHORING.md`,
`CLI_ARCHITECTURE.md`, and `CONTRIBUTING.md`. For how the spec machinery turns a
`Step` into a CLI command and a TUI form, see
[docs/concepts/spec-and-cli-generation.md](../concepts/spec-and-cli-generation.md).

The procedure is validator-gated: `make check` (lint + format + generated-docs
freshness + tests, see the `check` target in `Makefile`) is the same gate CI runs.
Run it at each gate below and only proceed when it passes.

## Contents

- [Where things live](#where-things-live)
- [The checklist](#the-checklist)
- [Two hard invariants](#two-hard-invariants)
- [Params: hand-written vs derived](#params-hand-written-vs-derived)
- [Adding a whole new example](#adding-a-whole-new-example)

## Where things live

| Path | Responsibility |
|---|---|
| `geneva_examples/core/spec.py` | The framework: `Param`, `Step`, `Example`, `COMMON_HELP`, `params_from_signature`, `resolve_config`, `build_command`. Read this first. |
| `geneva_examples/core/config.py` | `Config` + `load_config` — parses `config.yaml` (mode, credentials, `db_uri`). |
| `geneva_examples/core/common.py` | Mode-aware plumbing every step uses: `connect`, `runtime_session`, `build_manifest`, `resolve_resources`, `local_or`, `local_concurrency`, `OPT_STABLE_ROW_IDS`, `require_stable_row_ids`, `format_sample`, `setup_logging`. |
| `geneva_examples/core/backfill.py` | `backfill_column()` — the shared drop/add/wait/backfill flow; the authoritative reset-vs-incremental contract. |
| `geneva_examples/examples/__init__.py` | The registry: `EXAMPLES = (IMAGES, VIDEO, PDF, AUDIO, DEBUGGING)`. Must stay import-cheap (no torch/geneva at import time). |
| `geneva_examples/examples/<pkg>/` | One package per example. There are five: `images/`, `video/`, `pdf/`, `audio/`, `debugging/`. |
| `geneva_examples/examples/<pkg>/__init__.py` | Defines the `Step`s and the `EXAMPLE` spec. Registration point #1. |
| `geneva_examples/examples/<pkg>/<step>.py` | One step module = one `run(cfg, *, ...)` function. |
| `geneva_examples/examples/<pkg>/<factory>.py`, `geneva_examples/examples/_shared/` | UDF/chunker factory modules, plain siblings of the step modules (e.g. `video/chunkers.py`, `_shared/clip.py`). There is no `udfs/` subdirectory anywhere in the repo. |
| `geneva_examples/examples/cli.py` | One `build_command(EXAMPLE, STEP)` line per command. Registration point #2. |
| `pyproject.toml` `[project.scripts]` | Maps `uv run <name>` to the `cli.py` attribute. Registration point #3. |
| `tests/` | The smoke tests the checklist requires (`test_pipeline_smoke.py`, `test_pipeline_ingest_smoke.py`, `test_pipeline_chunk_smoke.py`, shared fakes in `tests/_fakes.py`). |
| `geneva_examples/tui/app.py` | The TUI renders from the same registry — a registered step appears there with no extra wiring. |

## The checklist

Copy this list into your PR description and work through it in order.

- [ ] **Write the step module** — `geneva_examples/examples/<pkg>/<step>.py` with a
  single entry point `run(cfg: Config, *, ...)`. First argument is `cfg: Config`;
  everything else is keyword-only with a type annotation and a default (that is
  what becomes CLI options and TUI fields). Nest heavy imports (`geneva`, `torch`,
  `av`, …) inside `run()` and inside UDF factory bodies — the registry must stay
  import-cheap. Use `connect(cfg)`, `retry_io(...)` for table writes, and wrap the
  whole backfill/refresh loop in one `runtime_session(conn, cfg)`
  (`geneva_examples/core/common.py`).
- [ ] **Build the `Step`** in `geneva_examples/examples/<pkg>/__init__.py`, setting
  every relevant field — including the three that older guides omitted:

  ```python
  MY_STEP = Step(
      key="my-step",                       # the command name
      title="Human title",
      description="Shown verbatim in --help and the TUI.",
      run=my_step.run,
      params=params_from_signature(my_step.run, help=COMMON_HELP | {...}),
      gpu=True,                            # UI hint: runs a model (CPU-only locally)
      requires="run ingest-… first",       # UI hint: prerequisite step
      default_mode=None,                   # or "local" to pin --mode for demos
  )
  ```

  Field semantics live in `geneva_examples/core/spec.py` (the authoritative
  contract). Then add `MY_STEP` to the `EXAMPLE.steps` tuple in the same file.
- [ ] **Add the command binding** — one line in `geneva_examples/examples/cli.py`:
  `my_step = build_command(<pkg>.EXAMPLE, <pkg>.MY_STEP)`.
- [ ] **Add the console script** — one line in `pyproject.toml`
  `[project.scripts]`: `my-step = "geneva_examples.examples.cli:my_step"`.
- [ ] **Regenerate entry points** — `uv sync`, then sanity-check with
  `uv run my-step --help`.
- [ ] **Gate**: run `make check`; only proceed when it passes.
- [ ] **Add the tests** (the step older checklists forgot):
  - Backfill stage → add a row to `STAGE_CASES` in `tests/test_pipeline_smoke.py`.
    The tuple shape is `(cli attr, step module path, initial columns, expected
    backfilled columns, extra args)`; the parametrized test drives the command
    through click's `CliRunner` and asserts the added + backfilled column sets.
  - Ingest step → add a case to `tests/test_pipeline_ingest_smoke.py` asserting
    `conn.create_kwargs[<table>]["storage_options"] == {OPT_STABLE_ROW_IDS: "true"}`
    (see invariant 1 below).
  - Chunker step → add **both** cases per `tests/test_pipeline_chunk_smoke.py`: a
    create+refresh case, and a refusal case with
    `FakeTable(stable_row_ids=False)` asserting the command fails, names the
    source table, and leaves no half-built view (see invariant 2 below).

  The mocking recipe (what to patch, on which module, with which runner) is in
  [docs/authoring/testing.md](testing.md).
- [ ] **Regenerate the docs** — `make docs` rebuilds the generated CLI reference
  (`docs/reference/cli/`, including the
  [command index](../reference/cli/index.md)),
  [docs/reference/worker-runtime-pins.md](../reference/worker-runtime-pins.md),
  and `llms.txt` / `llms-full.txt` — the last concatenates every docs page, so
  editing any hand-written page also needs a regeneration. `make check` includes
  `make docs-check`, so skipping this fails the gate.
- [ ] **Final gate**: run `make check`; the change is done only when it passes.

## Two hard invariants

These two rules were previously stated only in test comments and source
docstrings. Every new step must satisfy them; the smoke tests in the checklist
are how they are enforced.

**1. Every created table passes stable row IDs.** Every `conn.create_table` call
in an ingest step passes `storage_options={OPT_STABLE_ROW_IDS: "true"}`
(`OPT_STABLE_ROW_IDS = "new_table_enable_stable_row_ids"`,
`geneva_examples/core/common.py`). Stable row IDs are write-time only — there is
no migration, only a full table rewrite — and any table may later become the
source of a chunker materialized view, so the option goes on unconditionally.
`tests/test_pipeline_ingest_smoke.py` asserts this for `ingest-images`,
`ingest-videos`, and `ingest-pdfs`. `ingest-videos-openvid`,
`ingest-videos-external`, `ingest-audio`, `seed-video-clips`, and `demo-errors`
pass the option in code but have no assertion yet — add one when you touch them.

**2. Every chunker step calls `require_stable_row_ids` before creating its
view.** A chunker materialized view pins the source version it was built against
and never advances it; a source without stable row IDs becomes permanently
unrefreshable the first time the source version moves — which the maintenance
agent's own compaction does without user action. `require_stable_row_ids(src,
source_table)` (`geneva_examples/core/common.py`) fails fast and names the
source table instead of leaving a view that dies later. See
[docs/concepts/materialized-views.md](../concepts/materialized-views.md) for the
full rationale;
`tests/test_pipeline_chunk_smoke.py::test_chunk_cli_refuses_source_without_stable_row_ids`
is the enforcing test.

## Params: hand-written vs derived

The default is to derive params from the `run()` signature:

```python
params=params_from_signature(run, help=COMMON_HELP | {"limit": "Max rows…"})
```

`params_from_signature` (`geneva_examples/core/spec.py`) reads name, type, and
default from each keyword-only argument (skipping `cfg`), so adding a keyword
argument to `run()` adds a CLI flag and a TUI field with no other edit. Merge
per-step help over `COMMON_HELP`; pass `choices=` / `bounds=` dicts for enum and
range validation. The video, pdf, and audio examples all use this form.

The escape hatch is a hand-written `Param` tuple, used when the spec needs
something the signature cannot express cleanly (shared param groups, bounds on
many fields). `geneva_examples/examples/images/__init__.py` is the reference: it
hand-writes every `Param` and never calls `params_from_signature`.

Either way, `build_command` consumes the already-built `step.params` — it never
derives params itself. Whichever form you choose, the params exist exactly once
and both front-ends render from them.

## Adding a whole new example

An example is a package exporting an `EXAMPLE` spec, plus one registry edit:

1. Create `geneva_examples/examples/<pkg>/` whose `__init__.py` defines the
   `Step`s and `EXAMPLE = Example(name=..., title=..., description=...,
   modality=..., steps=(...))`. `modality` is a free-form UI hint (the registry
   holds `image`, `video`, `pdf`, `audio`, and `demo`).
2. Register it in `geneva_examples/examples/__init__.py`: import the package's
   `EXAMPLE` and add it to the `EXAMPLES` tuple. This single edit makes the
   example visible to the CLIs, the TUI, and the docs generator.
3. Add the per-step `build_command` lines in `geneva_examples/examples/cli.py`
   and the `[project.scripts]` entries, then `uv sync` — i.e. run the full
   checklist above for each step.
4. Keep the package import-cheap: declaring the spec must not import torch or
   geneva. `tests/test_registry.py::test_registry_import_is_cheap` asserts this
   in a fresh subprocess and will fail the gate otherwise. The same file pins the
   registry order — extend its expected tuple when you add an example.
