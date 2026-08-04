# Environment variables

> Part of the geneva-examples docs — index: [docs/README.md](../README.md).

Every environment variable the repo reads or sets, in one place. Variables fall
into four groups: ones you may set, ones the code sets for you, the worker-side
credential contract for the assets bucket, and CI-only variables.

## Contents

- [Variables you may set](#variables-you-may-set)
- [Variables the code sets for you](#variables-the-code-sets-for-you)
- [Worker-side contract: the assets bucket](#worker-side-contract-the-assets-bucket)
- [CI-only variables](#ci-only-variables)
- [Configuration is YAML-only](#configuration-is-yaml-only)

## Variables you may set

| Variable | Default | Read by | Effect |
|---|---|---|---|
| `MMS_TTS_MODEL_ID` | `facebook/mms-tts-eng` | `geneva_examples/examples/audio/tts.py:32` | Overrides the TTS checkpoint for `synthesize-audio`. There is deliberately no CLI flag. The replacement model must emit 16 kHz audio — `setup()` raises `RuntimeError` on any other rate, because the downstream Whisper stage assumes 16 kHz (`geneva_examples/examples/audio/tts.py:107-115`). |
| `{PACKAGE}_PACKAGE_SPEC` | per-package | `geneva_examples/core/package_specs.py` and the manifest modules | Replaces one worker pip spec verbatim (it need not be an `==` pin). The full matrix of variables, current values, and defining modules is generated — see [docs/reference/worker-runtime-pins.md](worker-runtime-pins.md); it is not duplicated here. The naming rule is `{DISTRIBUTION}_PACKAGE_SPEC` with non-alphanumerics collapsed to `_` (`_default_env_var`, `geneva_examples/core/package_specs.py:21-28`), and the one deviation (OpenCLIP reads `OPEN_CLIP_PACKAGE_SPEC`, not the rule-derived `OPEN_CLIP_TORCH_PACKAGE_SPEC`) is listed on that page. |
| `HF_TOKEN` | unset | Hugging Face libraries (not this repo's code) | Raises HF download rate limits. Prefer setting `hf_token` in `config.yaml`: the code then exports it for you — `seed-video-clips` sets `HF_TOKEN` on the driver via `setdefault` (`geneva_examples/examples/video/seed.py:264-265`), and `chunk-videos-openvid` injects it into the worker environment through the manifest's `env_vars` (`geneva_examples/examples/video/chunk_openvid.py:110-117`). An ambient `HF_TOKEN` you export yourself is honored by the HF libraries on the driver, but is never shipped to enterprise workers. |

## Variables the code sets for you

The driver-side sets all use `os.environ.setdefault`, so a value you export
before running wins — with two exceptions: the TUI forces `PYTHONUNBUFFERED=1`
on its step subprocess (`geneva_examples/tui/app.py:970`), and values shipped
through a manifest's `env_vars` always win on the worker.

| Variable | Value set | Set by | Why |
|---|---|---|---|
| `RAY_ENABLE_UV_RUN_RUNTIME_ENV` | `0` | `resolve_config` (`geneva_examples/core/spec.py:176`), defensively repeated in step `run()` bodies and `geneva_examples/ops/cleanup.py` | Disables Ray's `uv run` runtime-env integration, which would otherwise package the whole working directory (HF caches, `local_db/`, …) and upload it — blowing past Ray's 512 MiB `working_dir` limit. `.rayignore` at the repo root is the second half of this defense: it keeps the heavy paths out of any `working_dir` upload should the integration ever be enabled. |
| `HF_HOME` | `./huggingface_cache` | the HF-downloading steps: `geneva_examples/examples/images/ingest.py:29`, `video/ingest.py:39`, `video/ingest_openvid.py:44`, `video/seed.py:266`, `audio/synthesize.py:52`, `audio/transcribe.py:118` | Keeps model weights and dataset downloads in a repo-local, gitignored cache instead of `~/.cache`. On `chunk-videos-openvid` workers it is set to `/tmp/hf_cache` via the manifest's `env_vars` (`geneva_examples/examples/video/chunk_openvid.py:114`) — a writable worker path. |
| `LANCE_LOG` | `warn` | `setup_logging` (`geneva_examples/core/common.py:53-54`), skipped when `--log-level DEBUG` | Silences lance's Rust `lance::events::*` INFO stream at the source. Must be set before lance is imported; workers inherit it from the driver environment. Pass `--log-level DEBUG` (or export `LANCE_LOG` yourself) to get it back. |
| `HF_TOKEN` | value of `hf_token` from `config.yaml` | `geneva_examples/examples/video/seed.py:264-265` (driver); `geneva_examples/examples/video/chunk_openvid.py:116` (worker `env_vars`) | Moves HF reads off the shared per-IP anonymous rate limit onto the authenticated quota — matters when many workers read from HF concurrently. |
| `PYTHONUNBUFFERED` | `1` | the TUI's step subprocess (`geneva_examples/tui/app.py:970`) | Streams the step's output line by line into the TUI log pane. Not a `setdefault` — the subprocess env forces it. |

## Worker-side contract: the assets bucket

The URI-streaming chunker reads the assets-bucket credentials from the **worker**
environment, never from the connection's storage options. All five keys are read
inside the UDF closure in `geneva_examples/examples/video/chunkers_uri.py:213-219`:

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `ASSETS_S3_ACCESS_KEY` | yes | — | access key for the assets bucket |
| `ASSETS_S3_SECRET_KEY` | yes | — | secret key for the assets bucket |
| `ASSETS_S3_ENDPOINT` | yes | — | bare endpoint host (no scheme) for `pyarrow.fs.S3FileSystem` |
| `ASSETS_S3_REGION` | no | `us-east-1` | SigV4 region |
| `ASSETS_S3_SCHEME` | no | `https` | `http` or `https` |

If a required key is missing on a worker, the UDF logs
`missing worker video credential env …` and skips the row rather than failing the
job (`geneva_examples/examples/video/chunkers_uri.py:220-224`).

How the values get there:

- **Enterprise mode**: `chunk-videos-external` injects them via the manifest's
  `env_vars` (`geneva_examples/examples/video/chunk_external_video.py`). Note that
  manifest `env_vars` are stored in plaintext in the manifest/job record — see
  [docs/authoring/writing-udfs.md](../authoring/writing-udfs.md).
- **Local mode**: the same step sets them in-process; local Ray workers share the
  driver environment.

These variables are a worker-side *output* of the CLI, not a driver-side input:
ambient `ASSETS_S3_*` values in your shell never satisfy the driver's credential
resolution, which reads only the `--video-*` flags and the `assets_s3_*` keys in
`config.yaml` (pinned by
`tests/test_video_external.py::test_resolve_video_creds_ignores_ambient_env`).
The storage-bucket `s3_*` credentials are deliberately never consulted either way
— see [docs/getting-started/configuration.md](../getting-started/configuration.md).

## CI-only variables

| Variable | Where | Purpose |
|---|---|---|
| `UV_INDEX_LANCEDB_PASSWORD`, `UV_INDEX_LANCE_FORMAT_PASSWORD` | documented in a comment in `.github/workflows/ci.yml:26-28`; not currently set | The fallback if the Gemfury package indexes ever require auth: add them as repo secrets and pass them as env to the `uv sync` step. |
| `TRUFFLEHOG_VERSION` | `.github/workflows/ci.yml:117` | Pins the TruffleHog binary (the only blocking security check) downloaded by the audit job. |

## Configuration is YAML-only

No environment variable selects the mode, the connection target, or any
credential. `Config` and `load_config` (`geneva_examples/core/config.py`) read
only the YAML file — the module contains zero `os.environ` reads. Everything
connection-shaped goes through `config.yaml` (or the `--config`/`--mode`/
`--db-uri` flags); see
[docs/getting-started/configuration.md](../getting-started/configuration.md) for
the full key reference and precedence rules.
