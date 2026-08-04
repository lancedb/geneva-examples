# Contributing to geneva-examples

Thanks for improving the Geneva UDF examples. Full contributor documentation
lives in [`docs/`](docs/README.md); this page is the short version.

## Local setup

Requires Python ≥ 3.12 and [`uv`](https://docs.astral.sh/uv/).

```bash
make install   # uv sync --group dev + install the git pre-commit hook
make check     # the full CI gate: lint + format + docs freshness + tests
```

`geneva`, `lancedb`, and `pylance` are pinned betas served from Gemfury indexes
(declared in [`pyproject.toml`](pyproject.toml)). If your environment can't
reach those indexes, `uv sync` will fail on those packages — request access or
run in an environment that has it. Never bump those pins (or `pyarrow`) without
the upgrade runbook:
[docs/operations/version-pins.md](docs/operations/version-pins.md).

Useful targets (see `make help`): `make lint-fix`, `make format`, `make test`,
`make docs`, `make typecheck`, `make audit`, `make precommit`.

## Conventions

- **Formatting & linting:** `ruff` (config in `pyproject.toml`) gates every
  commit via pre-commit. Note the pre-commit hook runs `ruff check --fix` — it
  rewrites your working tree. Line length is 88.
- **Type checking:** `ty` runs in pre-commit and CI but is **non-blocking** by
  design (preview tool, false positives on untyped ML deps). For opaque
  Geneva/LanceDB runtime objects use the structural `Protocol`s in
  [`geneva_examples/core/_types.py`](geneva_examples/core/_types.py).
- **UDF closures:** a UDF/chunker body ships to remote workers — nest its
  imports and helpers inside the factory function. The full rules:
  [docs/authoring/writing-udfs.md](docs/authoring/writing-udfs.md).
- **Dependencies:** after editing `pyproject.toml`, run `make lock` — CI's
  `uv sync --locked` fails on an out-of-date lockfile.
- **Commits:** history follows conventional commits with scopes
  (`feat(tui):`, `fix(deps):`, `docs(backfill):`, `test(tui):`).
- **Generated docs:** `docs/reference/cli/`, `docs/reference/worker-runtime-pins.md`,
  `llms.txt`, and `llms-full.txt` are generated — never edit them. Run
  `make docs` after any spec, pin, or docs-page change (`llms-full.txt`
  concatenates the whole docs tree).

## Adding a new example or step

Follow the single checklist in
[docs/authoring/adding-a-step.md](docs/authoring/adding-a-step.md) (prototype
first in UDF Studio: [docs/workflows/udf-studio.md](docs/workflows/udf-studio.md)).

## Testing

The suite runs without a cluster, GPU, or model weights; the Geneva boundary
is mocked. The harness, the smoke-test recipe, the 90% coverage-gate
mechanics (including `--no-cov` for focused runs), and the omit-list policy:
[docs/authoring/testing.md](docs/authoring/testing.md).

Run `make check` before opening a PR.
