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

## Adding a new example

Examples are self-contained packages under
[`geneva_examples/examples/`](geneva_examples/examples/). Each declares a **spec**
(`Example` → `Step`s → `Param`s) that both the generated CLIs and the TUI render,
so params and descriptions are defined once.

1. **Prototype in UDF Studio.** Run `uv run udf-studio`, pick a template, point it
   at sample data in `studio_data/`, and iterate on your `transform(value)` (UDF)
   or `chunk(value)` (chunker) locally — no cluster, GPU, or Ray. See the
   [README](README.md#udf-studio).
2. **Add the UDF factory + manifest.** In your example package (new or existing),
   add a UDF module following
   [`examples/images/imageinfo.py`](geneva_examples/examples/images/imageinfo.py):
   a `build_*_udf(...)` factory and a `*_RUNTIME_PIP` list pinning the worker-side
   packages (env-overridable, like `GENEVA_PACKAGE_SPEC`). Shared model UDFs go in
   [`examples/_shared/`](geneva_examples/examples/_shared/).
3. **Add a step run-function.** Write `run(cfg: Config, *, ...) -> None` modeled on
   [`examples/images/lightweight.py`](geneva_examples/examples/images/lightweight.py):
   `connect(cfg)`, `build_manifest(cfg, ...)` (→ `None` locally), build the UDF(s)
   via `resolve_resources(cfg, ...)`, and call
   [`backfill_column()`](geneva_examples/core/backfill.py) inside
   `runtime_session(conn, cfg)`. Heavy imports stay nested inside `run`.
4. **Declare the spec.** In the example's `__init__.py`, add a `Step` (title +
   markdown description + `params=params_from_signature(run, help=...)`), and list
   it on the `Example`. Register a brand-new example in
   [`examples/__init__.py`](geneva_examples/examples/__init__.py) — the TUI picks
   it up automatically.
5. **Expose the CLI.** Add a `build_command(...)` binding in
   [`examples/cli.py`](geneva_examples/examples/cli.py) and a `project.scripts`
   entry in `pyproject.toml`.

## Testing & the coverage policy

The suite enforces a **90% coverage gate**, but the pieces that need a live
cluster, GPU, or model weights are listed in `[tool.coverage.run] omit` in
`pyproject.toml` (the model UDFs, the pipeline/ops CLIs, the Gradio wiring). Their
*pure* helpers are still unit-tested — they just don't inflate the percentage.

When you add code:

- Unit-test pure helpers directly (see `tests/test_udfs.py`,
  `tests/test_pipeline_runner.py`, `tests/test_spec.py`, `tests/test_ops_*.py`).
- Registry + spec invariants live in `tests/test_registry.py` /
  `tests/test_spec.py`; TUI behavior in `tests/test_tui.py` (Textual pilot) and
  `tests/test_tui_forms.py`.
- For CLI *wiring* that would otherwise hit a cluster, add a mocked smoke test in
  the style of [`tests/test_pipeline_smoke.py`](tests/test_pipeline_smoke.py):
  drive the generated command from `geneva_examples.examples.cli` with `click`'s
  `CliRunner` in `--mode local`, monkeypatch the step module's `connect`, and (for
  model steps) use the injected fake `geneva` module.
- Reuse the synthetic-media fixtures in `tests/conftest.py`
  (`make_png`, `make_mp4`, `data_dir`).

Run `make check` before opening a PR.
