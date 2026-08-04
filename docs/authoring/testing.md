# Testing guide

> Part of the geneva-examples docs — index: [docs/README.md](../README.md).

How this repo tests pipelines that normally need a cluster, a GPU, and model
weights — and the local conventions (coverage gate, hygiene rules, CI gates)
that a new test must follow. The required tests for a new step are listed in the
checklist in [docs/authoring/adding-a-step.md](adding-a-step.md); this page
explains how to write them.

## Contents

- [Two tiers of mocking](#two-tiers-of-mocking)
- [The smoke-test recipe](#the-smoke-test-recipe)
- [Fixtures](#fixtures)
- [Coverage gate mechanics](#coverage-gate-mechanics)
- [Test hygiene rules](#test-hygiene-rules)
- [Lint, type checks, and CI](#lint-type-checks-and-ci)

## Two tiers of mocking

The cluster boundary is mocked at two different depths; picking the wrong tier
is the most common way to write a broken test.

**Tier 1 — fake geneva.** `tests/_fakes.py` provides recording stand-ins
(`FakeConn`, `FakeTable`, `FakeManifest`) plus `install_fake_geneva(monkeypatch)`,
which injects a synthetic `geneva` + `geneva.manifest` module pair into
`sys.modules` (a version string, a pass-through `udf` decorator, a
`skip_on_error` stub, and `FakeManifest` behind the
`create_pip(...).pip(...).build()` chain). It is exposed as the `fake_geneva`
fixture in `tests/conftest.py`. Use this tier for steps whose geneva usage is
just `@geneva.udf` + connection calls — the backfill stages in
`tests/test_pipeline_smoke.py` are the template.

**Tier 2 — real geneva, patched connection.** Steps that call real geneva
machinery the fake does not provide — the PDF steps (`geneva.udfs.document`),
the video chunkers (`@geneva.chunker`), and the stable-row-ID guard
(`geneva.db.dataset_uses_stable_row_ids`) — cannot run against it: building the
chunker/UDF itself must be real. (Manifest builds are not on this list — the
Tier-1 fake supplies `GenevaManifest`.) These tests do **not** use
`fake_geneva`; they import real geneva and patch only `connect` and
`runtime_session`. `tests/test_pipeline_chunk_smoke.py` and the PDF case in
`tests/test_pipeline_smoke.py` (which carries the explanatory comment) are the
templates; the ingest smoke tests (`tests/test_pipeline_ingest_smoke.py`) and
`tests/test_video_external.py` are also in this tier. A chunker-based example
must follow this tier.

Consequence of Tier 2: the suite still **requires the Gemfury-pinned
`geneva`/`lancedb`/`pylance` installed** (see
[docs/getting-started/install.md](../getting-started/install.md)). "No cluster,
GPU, or model weights" is accurate; "no geneva" is not — if `uv sync` fails on
the package indexes, the tests cannot run at all.

## The smoke-test recipe

The universal pattern for driving a generated step command end to end:

```python
import importlib
from contextlib import nullcontext

from _fakes import FakeConn, FakeTable
from click.testing import CliRunner

from geneva_examples.examples import cli


def test_my_step_wires_backfill(monkeypatch, fake_geneva):
    mod = importlib.import_module("geneva_examples.examples.video.my_step")
    table = FakeTable(names=["frame"])
    monkeypatch.setattr(
        mod, "connect", lambda _cfg: FakeConn(table=table, is_remote=False)
    )
    monkeypatch.setattr(mod, "runtime_session", lambda *_a, **_k: nullcontext())

    result = CliRunner().invoke(
        cli.my_step, ["--mode", "local", "--schema-wait-sleep-s", "0"]
    )

    assert result.exit_code == 0, result.output
    assert set(table.backfilled) == {"my_column"}
```

The load-bearing details:

- **Patch `connect` on the step module, not on `core.common`.** Each step module
  does `from geneva_examples.core.common import connect`, so the name is rebound
  per module — patching `geneva_examples.core.common.connect` changes nothing
  the step sees.
- **Patch `runtime_session` to `nullcontext()`** (the `_no_ray` helper in
  `tests/test_pipeline_smoke.py`) so no real local Ray instance starts.
- **Run in `--mode local` with `--schema-wait-sleep-s 0`** so the
  `wait_for_columns` polling loop does not sleep.
- **Generated step CLIs are click commands** — use `click.testing.CliRunner` and
  invoke the attribute on `geneva_examples/examples/cli.py`. **Ops CLIs
  (`stats`/`jobs`/`cleanup`) are Typer apps** — use `typer.testing.CliRunner`,
  invoke `<module>.app`, and additionally patch `load_config` on the ops module
  (see `tests/test_ops_smoke.py`).
- For a standard backfill stage, prefer adding a row to `STAGE_CASES` in
  `tests/test_pipeline_smoke.py` over a bespoke test — the tuple shape is
  `(cli attr, step module path, initial columns, expected backfilled columns,
  extra args)`.

Two `FakeTable` behaviors any replacement fake must preserve
(`tests/_fakes.py`): `add_columns` reflects new names into `schema.names` so
`wait_for_columns()` sees them and returns instead of timing out, and
`to_lance()` returns an object with `has_stable_row_ids` so
`FakeTable(stable_row_ids=False)` can drive the chunker source guard negative.
`FakeConn.create_kwargs` records per-table `create_table` kwargs, which is how
ingest tests assert the stable-row-ID invariant.

## Fixtures

Defined in `tests/conftest.py` (verify there; this table is the summary):

| Name | Kind | Provides |
|---|---|---|
| `fake_geneva` | fixture | Installs the Tier-1 fake geneva modules (wraps `install_fake_geneva`). |
| `data_dir` | fixture | A `studio_data`-shaped tmp tree: `images/`, `videos/`, `audio/`, `pdfs/`, `input.csv`, plus decoy `.txt` files for extension-filter tests. |
| `library_path` | fixture | A fresh local LanceDB UDF-library location (created on first save). |
| `mp4_bytes` | fixture | A tiny 3-second in-memory H.264 mp4. |
| `pdf_bytes` | fixture | A tiny single-page text PDF. |
| `make_png_bytes` | fixture (factory) | Callable returning PNG bytes of a given `(width, height)`. |
| `make_png` / `make_mp4` / `make_pdf` | module-level helpers, **not** fixtures | Build the synthetic media the fixtures above are made from. |

Earlier contributor docs misnamed the helpers as fixtures. In a test, request
the fixtures; the helper functions are internal to `conftest.py` (the byte
fixtures cover the common needs, including PDFs via `make_pdf`'s hand-assembled
xref — no reportlab dependency).

## Coverage gate mechanics

The coverage gate is enforced by pytest itself, not by CI or the Makefile:
`[tool.pytest.ini_options] addopts` in `pyproject.toml` includes
`--cov=geneva_examples` and `--cov-fail-under=90`, and CI's test step is a bare
`uv run pytest`.

**The `--no-cov` gotcha:** because the gate lives in `addopts`, any focused run
— `uv run pytest tests/test_spec.py`, `-k something` — computes coverage over
the whole package from that subset and fails the 90% gate even when every
selected test passes. Pass `--no-cov` for focused runs:

```bash
uv run pytest tests/test_spec.py --no-cov
```

**The omit list is a policy document.** `[tool.coverage.run] omit` in
`pyproject.toml` excludes files whose bodies need a live cluster, Ray, GPUs,
weights, or a browser/terminal (step `run()` bodies, ops CLIs, the TUI, model
UDF modules). Their pure helpers are still unit-tested and their CLI wiring is
smoke-tested — the omission is a metric decision only. When you add a file whose
body cannot run under test, **editing the omit list with a justification comment
is an expected part of the PR**, following the existing annotated entries.

## Test hygiene rules

- **Always pass an explicit `--config <tmp_path>/...`** (a missing file is fine
  — that resolves to defaults) in any test that touches config or credential
  resolution. `load_config` defaults to `./config.yaml` relative to the CWD, so
  without this, a developer's real gitignored `config.yaml` leaks into the test
  — the failure mode is a green local run and a red CI. See the helpers in
  `tests/test_pipeline_chunk_smoke.py` and `_stub_config` in `tests/test_tui.py`.
- **Preset stale worker-env values when testing env transport.** Tests that
  assert `ASSETS_S3_*` writes first `monkeypatch.setenv(key, "stale")` for every
  key: monkeypatch then restores the ambient env afterwards, and the assertions
  prove the step's writes win over ambient values
  (`tests/test_pipeline_chunk_smoke.py`).
- **Import the fakes bare.** `tests/` has no `__init__.py`, so pytest's default
  import mode puts the tests directory on `sys.path`; test modules and
  `conftest.py` do `from _fakes import FakeConn, FakeTable`. Follow that
  convention, not a package-relative import.
- **Suppress lint on literal fake secrets.** Ruff's bandit rules are enabled; a
  test containing a literal fake credential needs `# noqa: S105`/`S106` (and a
  subprocess call needs `# noqa: S603`), matching existing usage.

## Lint, type checks, and CI

- **Pre-commit's ruff hook rewrites your tree.** The `ruff-check` hook runs
  `uv run ruff check --fix` (`.pre-commit-config.yaml`), so committing can
  modify your files — re-stage and commit again. This interacts badly with
  partially staged hunks. `ruff format` runs as a second hook.
- **`ty` is non-blocking everywhere** (manual pre-commit stage,
  `continue-on-error` in CI, informational Makefile target) — it is a 0.0.x
  preview with false positives on untyped ML dependencies. Run it with
  `make typecheck`.
- **Run `make lock` after editing dependencies.** CI installs with
  `uv sync --locked --group dev` (`.github/workflows/ci.yml`), which fails on
  any `pyproject.toml` dependency edit that was not re-locked — and the failure
  message does not say "run `make lock`". This gate protects the exact `==`
  cluster pins; see [docs/operations/version-pins.md](../operations/version-pins.md)
  before touching those.
- **TruffleHog is the only blocking security check, and it scans full git
  history.** The CI `audit` job checks out with `fetch-depth: 0` and runs
  `trufflehog git file://. --only-verified --fail`; a leaked-then-reverted
  credential still fails CI and needs history rewriting, not a follow-up commit.
  `pip-audit` in the same job is advisory (`continue-on-error`).
- **`make check` is the local CI gate**: lint + format check + generated-docs
  freshness (`make docs-check`) + tests. It does not run `typecheck` or `audit`.
