# Contributing to geneva-examples

Thanks for improving the Geneva UDF examples. This guide covers the local setup,
the project's conventions, and the workflow for adding a new UDF or stage.

## Local setup

Requires Python ≥ 3.12 and [`uv`](https://docs.astral.sh/uv/).

```bash
make install   # uv sync --group dev + install the git pre-commit hook
make check     # the full CI gate: ruff lint + format-check + pytest (90% coverage)
```

`geneva`, `lancedb`, and `pylance` are pinned betas served from Gemfury indexes
(declared in [`pyproject.toml`](pyproject.toml)). If your environment can't reach
those indexes, `uv sync` will fail on those packages — request access or run in an
environment that has it.

Useful targets (see `make help`): `make lint-fix`, `make format`, `make test`,
`make typecheck`, `make precommit`.

## Conventions

- **Formatting & linting:** `ruff` (config in `pyproject.toml`) is the single
  source of truth and gates every commit via pre-commit. Run `make format` before
  pushing. Line length is 88; existing `# noqa` suppressions are kept honest by
  `RUF100`, so don't add ones the selected rules won't use.
- **Type checking:** `ty` runs in pre-commit and CI but is **non-blocking** by
  design — it's a preview tool with many false positives on the untyped ML deps.
  Prefer precise annotations; for the opaque Geneva/LanceDB runtime objects, use
  the structural `Protocol`s in
  [`geneva_examples/core/_types.py`](geneva_examples/core/_types.py) under a
  `TYPE_CHECKING` guard instead of importing the beta runtime types. Promoting
  `ty` to a blocking gate is deliberately deferred until its false-positive rate
  drops.
- **Imports in UDF bodies:** a UDF/chunker body is a self-contained closure that
  ships to the remote workers. Nest its imports and helpers *inside* the factory
  function so they serialize with it; keep the driver/CLI code lightweight.

## Adding a new UDF

1. **Prototype in UDF Studio.** Run `uv run udf-studio`, pick a template, point it
   at sample data in `studio_data/`, and iterate on your `transform(value)` (UDF)
   or `chunk(value)` (chunker) locally — no cluster, GPU, or Ray. See the
   [README](README.md#udf-studio).
2. **Add a factory + manifest.** Create a module in
   [`geneva_examples/udfs/`](geneva_examples/udfs/) following
   [`imageinfo.py`](geneva_examples/udfs/imageinfo.py): export a `build_*_udf(...)`
   factory and a `*_RUNTIME_PIP` list pinning the worker-side packages
   (env-overridable, like `GENEVA_PACKAGE_SPEC`).
3. **Wire a stage CLI.** Add a Typer CLI under
   [`geneva_examples/pipeline/stages/`](geneva_examples/pipeline/stages/) modeled on
   [`lightweight.py`](geneva_examples/pipeline/stages/lightweight.py): load config,
   `connect`, build a `GenevaManifest`, build the UDF(s), and call the shared
   [`backfill_column()`](geneva_examples/pipeline/stages/_runner.py) runner. Add a
   `project.scripts` entry in `pyproject.toml`.

## Testing & the coverage policy

The suite enforces a **90% coverage gate**, but the pieces that need a live
cluster, GPU, or model weights are listed in `[tool.coverage.run] omit` in
`pyproject.toml` (the model UDFs, the pipeline/ops CLIs, the Gradio wiring). Their
*pure* helpers are still unit-tested — they just don't inflate the percentage.

When you add code:

- Unit-test pure helpers directly (see `tests/test_udfs.py`,
  `tests/test_pipeline_runner.py`, `tests/test_ops_*.py`).
- For CLI *wiring* that would otherwise hit a cluster, add a mocked smoke test in
  the style of [`tests/test_pipeline_smoke.py`](tests/test_pipeline_smoke.py): use
  `typer.testing.CliRunner` and monkeypatch `load_config`/`connect` plus an
  injected fake `geneva` module.
- Reuse the synthetic-media fixtures in `tests/conftest.py`
  (`make_png`, `make_mp4`, `data_dir`).

Run `make check` before opening a PR.
