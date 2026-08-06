# geneva-examples — Geneva UDF examples

A self-contained set of **example UDF pipelines** and the **tooling** to run
them with Geneva, LanceDB's distributed UDF engine. Every example runs in two
modes with the same code: **local** (an on-disk Lance database + local Ray —
no cloud account, no cluster, no secrets) and **enterprise** (LanceDB
Enterprise + a remote Geneva runtime with GPU-backed workers).

## What's here

Five example pipelines (in
[`geneva_examples/examples/`](geneva_examples/examples/)), each a spec of
steps that generates both a `uv run <command>` CLI and a TUI form:

- **images** — ingest from Hugging Face, then file size/dimensions, OpenCLIP
  embeddings, BLIP captions
- **video** — ingest (bytes, OpenVid pointers, or an external S3 bucket),
  chunk into clips, then per-frame embeddings/captions/OpenPose skeletons
- **pdf** — per-page text + overlapping chunks via Geneva's shipped document
  UDFs
- **audio** — a text → speech → text round trip: MMS-TTS synthesis, Whisper
  transcription, WAV export
- **debugging** — a deliberately failing backfill that seeds real rows in the
  `geneva_errors` system table to explore

Plus a Textual **TUI** (`uv run tui`), inspection CLIs (`stats`, `jobs`,
`cleanup`), and **UDF Studio** (`uv run udf-studio`), a Gradio sandbox for
prototyping UDFs before wiring them into a step.

## Quickstart

Requires Python ≥ 3.12 and [`uv`](https://docs.astral.sh/uv/). With no
`config.yaml` at all, every command runs in local mode — a fresh checkout
works with zero configuration:

```bash
make install          # uv sync + pre-commit hook
uv run ingest-images  # create ./local_db and load sample images
uv run lightweight    # backfill file_size + dimensions (local Ray, CPU)
uv run embed          # backfill OpenCLIP embeddings on CPU
uv run tui            # or browse, tune, and run every step interactively
```

## Documentation

Full documentation lives in [`docs/README.md`](docs/README.md) — one page per
topic, with a generated CLI reference. Agents: fetch [`llms.txt`](llms.txt)
for a machine-readable index.

| Common tasks | Page |
| --- | --- |
| Configure modes, credentials, `config.yaml` keys | [docs/getting-started/configuration.md](docs/getting-started/configuration.md) |
| Run a pipeline (images, video, pdf, audio, debugging) | [docs/workflows/](docs/README.md#workflows) |
| Every command's flags and defaults (generated) | [docs/reference/cli/index.md](docs/reference/cli/index.md) |
| Add your own step or UDF | [docs/authoring/adding-a-step.md](docs/authoring/adding-a-step.md) |
| Something is broken | [docs/operations/troubleshooting.md](docs/operations/troubleshooting.md) |
| What a term means | [docs/reference/glossary.md](docs/reference/glossary.md) |

## Modes in one paragraph

Every CLI takes `--mode {local|enterprise}`; the mode otherwise comes from
`config.yaml`, defaulting to local. Local mode needs nothing and is tuned to
fit a small machine (model stages run on CPU — correct, just slower).
Enterprise mode needs a LanceDB Enterprise API key, region, and a reachable
Geneva host in `config.yaml` (copy
[`config-example-enterprise.yaml`](config-example-enterprise.yaml)); UDF
backfills then execute on remote workers. Details, precedence rules, and
every config key: [docs/getting-started/configuration.md](docs/getting-started/configuration.md).

## Development

```bash
make check   # the CI gate: lint + format + docs freshness + tests (90% coverage)
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and conventions,
[docs/authoring/testing.md](docs/authoring/testing.md) for the test harness,
and run `make help` for all targets.

## License

Apache-2.0 (see [LICENSE](LICENSE)). The example commands download
third-party datasets and models (Hugging Face datasets, OpenCLIP/BLIP/MMS/
Whisper weights) that are governed by their own licenses and terms.
