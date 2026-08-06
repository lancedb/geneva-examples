# Install

> Part of the geneva-examples docs — index: [docs/README.md](../README.md).

## Prerequisites

- Python `>=3.12,<3.13` (`requires-python` in `pyproject.toml`).
- [`uv`](https://docs.astral.sh/uv/) — every command in this repo runs through it.
- Nothing else for local mode: no cluster, no GPU, no cloud account, no secrets.
  Local runs clamp every resource request to the machine itself — no GPUs, at
  most its core count, at most a quarter of its RAM — so even a 2 GB / 4-core box
  can schedule the work; the exact clamping rules are in
  [docs/reference/local-mode.md](../reference/local-mode.md).
- Enterprise mode additionally needs a LanceDB Enterprise API key, a region, and a
  reachable Geneva host URL — see
  [docs/getting-started/configuration.md](configuration.md).

## Install

Run `make install`. It does two things:

```bash
uv sync --group dev          # resolve and install the locked env, dev tools included
uv run pre-commit install    # install the git pre-commit hook (ruff lint/format)
```

`uv sync` alone is enough to run the examples if you don't plan to commit changes.
Everything downstream (`uv run ingest-images`, `uv run tui`, `make test`, ...) uses
the virtualenv that sync creates.

The Makefile is self-documenting: run `make help` for the full target list — it is
the source of truth for development commands, so they are not duplicated here.

## The package indexes

`geneva`, `lancedb`, and `pylance` are pinned beta releases that do not exist on
public PyPI (PyPI carries only geneva's final releases). `pyproject.toml` declares
two Gemfury indexes under `[[tool.uv.index]]` and routes exactly those three
packages to them via `[tool.uv.sources]`:

| Index name | URL | Serves |
| --- | --- | --- |
| `lancedb` | `https://pypi.fury.io/lancedb/` | `geneva` and `lancedb` betas |
| `lance-format` | `https://pypi.fury.io/lance-format/` | `pylance` betas |

Both indexes are `explicit = true`, so every other dependency still resolves from
PyPI, and `prerelease = "allow"` lets the beta pins resolve at all. `uv` handles
the routing automatically — no extra flags.

If your network cannot reach the Gemfury indexes, `uv sync` fails on those three
packages (a fetch error for `pypi.fury.io`, or — if the index is reachable but
missing the pin — a no-matching-version "unsatisfiable" resolution error). There
is no PyPI fallback for the betas; request access or run in an environment that
has it (see `CONTRIBUTING.md`).

The pin values themselves are machine-derived — read the current versions in
[docs/reference/worker-runtime-pins.md](../reference/worker-runtime-pins.md), and
see [docs/operations/version-pins.md](../operations/version-pins.md) for why this
repo's pins must match the deployed Geneva driver exactly and how to upgrade them.

## What running examples downloads

The install itself downloads only Python packages. Running example steps then
fetches third-party datasets and model weights on first use:

| What | Downloaded by | Cached at |
| --- | --- | --- |
| `timm/oxford-iiit-pet` images (Hugging Face) | `ingest-images` (default dataset) | `./huggingface_cache` |
| `lance-format/openvid-lance` dataset (Hugging Face, via `hf://`) | `ingest-videos-openvid` (reference-only rows; raw video bytes stay remote) | `./huggingface_cache` (the step sets `HF_HOME`; the Lance dataset itself is scanned remotely over `hf://`, so no full local copy is guaranteed) |
| Sintel demo MP4 (archive.org) | `ingest-videos` | `./video_cache` |
| MMS-TTS and Whisper model weights | first run of `synthesize-audio` / `transcribe-audio` | `./huggingface_cache` |
| OpenCLIP, BLIP, and OpenPose model weights | first run of `embed`, `caption`, `frame-*` | the model libraries' default caches (e.g. `~/.cache/huggingface`) |

The ingest and audio steps set `HF_HOME=./huggingface_cache` themselves (see
[docs/reference/environment-variables.md](../reference/environment-variables.md));
the repo-local cache directories (`./huggingface_cache`, `./video_cache`) are
gitignored and safe to delete.

Licensing note: this repository is Apache-2.0 (`LICENSE`), but the datasets and
model weights the example commands download are third-party and governed by their
own licenses and terms of use. Review them before redistributing anything derived
from the outputs.

## Verify

```bash
make check     # the CI gate: ruff lint + format check + generated-docs freshness + tests
uv run tui     # the interactive runner starts; its Examples nav lists all five examples
```

`make check` needs no cluster, GPU, or credentials — the test suite mocks the
geneva boundary (see [docs/authoring/testing.md](../authoring/testing.md)). From
here, run your first pipeline: [docs/workflows/images.md](../workflows/images.md).
