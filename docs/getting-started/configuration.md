# Configuration

> Part of the geneva-examples docs — index: [docs/README.md](../README.md).

All configuration lives in one YAML file — `config.yaml` by default — loaded by
`geneva_examples/core/config.py`. This page is the canonical reference for every
key, the mode and URI resolution rules, and the credential model. Every claim is
verified against `geneva_examples/core/config.py`; the executable spec is
`tests/test_config.py`.

## Contents

- [Modes at a glance](#modes-at-a-glance)
- [Mode and db_uri precedence](#mode-and-db_uri-precedence)
- [config.yaml key reference](#configyaml-key-reference)
- [The two credential sets](#the-two-credential-sets)
- [db_uri normalization](#db_uri-normalization)
- [Object-store regions](#object-store-regions)
- [Table names are not configuration](#table-names-are-not-configuration)
- [Templates](#templates)

## Modes at a glance

Geneva powers both modes; the `mode` switch decides where the database lives and
where backfill compute runs (`geneva_examples/core/common.py`, `connect()`).

| | Local mode | Enterprise mode |
| --- | --- | --- |
| Connection target | On-disk Lance database at `local_db_path` (default `./local_db`) | LanceDB Cloud at `db_uri` (`db://...`) plus the Geneva runtime at `geneva_host` |
| Where backfills run | One local Ray instance, provisioned per run on the driver machine | Remote Ray workers in the Geneva runtime |
| Config file | Optional — a missing `config.yaml` resolves to local mode with defaults | Required — must exist and set `lancedb_api_key`, `lancedb_region`, `geneva_host` (`geneva_examples/core/config.py`, `load_config`) |
| Secrets required | None | `lancedb_api_key` at minimum; storage bucket credentials when the client writes Lance data to your own bucket |
| `db_uri` | Accepted but unused | The connection target, normalized (see [db_uri normalization](#db_uri-normalization)) |

For exactly how local mode clamps resource knobs (`num_gpus`, memory, concurrency,
...), see [docs/reference/local-mode.md](../reference/local-mode.md).

## Mode and db_uri precedence

The mode is resolved by `resolve_mode()` (`geneva_examples/core/config.py:121`)
with this precedence:

1. the `--mode` flag on the command (its click default is the step's
   `default_mode`, which is unset for every step except `demo-errors`);
2. else the config file's `mode` key;
3. else inferred: `enterprise` when `geneva_host` is set, otherwise `local`.

The value is lowercased before checking, so `mode: Local` works. Anything outside
`local`/`enterprise` raises `RuntimeError("invalid mode ...")`.

`db_uri` is resolved as `--db-uri` flag, else the file's `db_uri` key, else the
default `db://quickstart`, and the result always passes through
`normalize_db_uri()` (`geneva_examples/core/config.py:222-224`). Because that
chain uses `or`, a blank `--db-uri ""` is ignored and falls through to the file
value — the `or` chain itself is the source for the blank-string case;
`tests/test_config.py::test_load_config_db_uri_override_ignored_when_blank` pins
the flag-not-passed `None` case.

Two pitfalls worth knowing before anything else:

- **The config path resolves against the current working directory.** `--config`
  defaults to `Path("config.yaml")` relative to wherever you ran the command, not
  the repo root (`geneva_examples/core/config.py:193-194`). A missing file is legal
  (it means local mode), so running a command from a subdirectory does not error —
  it silently falls back to local-mode defaults and creates `./local_db` there.
  If a command unexpectedly ran locally, check your working directory first.
- **`--db-uri` is accepted but unused in local mode.** Local connections open
  `local_db_path`; the resolved `db_uri` is ignored with no warning
  (`geneva_examples/core/common.py`, `connect()`; `normalize_db_uri()` returns
  early for non-enterprise modes).

## config.yaml key reference

All 18 keys of the `Config` dataclass
(`geneva_examples/core/config.py:54-84`). Every key is optional in local mode;
the "Required" column describes enterprise mode.

| Key | Type | Default | Required (enterprise) | Description |
| --- | --- | --- | --- | --- |
| `mode` | str | inferred (see [precedence](#mode-and-db_uri-precedence)) | no | `local` or `enterprise`. |
| `lancedb_api_key` | str | — | yes | LanceDB Enterprise API key, passed to `geneva.connect(api_key=...)`. |
| `lancedb_region` | str | — | yes | LanceDB Enterprise region, passed to `geneva.connect(region=...)`. See [Object-store regions](#object-store-regions). |
| `geneva_host` | str | — | yes | Reachable Geneva runtime URL. Its presence alone infers enterprise mode. |
| `db_uri` | str | `db://quickstart` | no | Enterprise connection target; normalized (see [db_uri normalization](#db_uri-normalization)). Unused in local mode. Override per command with `--db-uri`. |
| `local_db_path` | str | `./local_db` | n/a (local only) | On-disk Lance database directory; `~` is expanded (`geneva_examples/core/common.py:143`). No CLI override exists. Unused in enterprise mode. |
| `s3_access_key` | str | — | no | Storage bucket access key. All four `s3_*` keys or none. |
| `s3_secret_key` | str | — | no | Storage bucket secret key. |
| `s3_endpoint` | str | — | no | Storage bucket S3-compatible endpoint URL. |
| `s3_region` | str | — | no | Storage bucket region. See [Object-store regions](#object-store-regions). |
| `aws_allow_http` | bool | `false` | no | Allow plain-HTTP object storage (e.g. MinIO) in the storage options. The only coerced key: `_as_bool()` normalizes strings against `{"true", "1", "yes", "on"}` because YAML parses a quoted `"false"` to a truthy string (`geneva_examples/core/config.py:42-51`). |
| `azure_account_name` | str | — | no | Azure storage account name — the Azure equivalent of the `s3_*` set. Requires `azure_account_key`. |
| `azure_account_key` | str | — | no | Azure storage account key. Requires `azure_account_name`. |
| `assets_s3_access_key` | str | — | no | Assets bucket access key. All four `assets_s3_*` keys or none. |
| `assets_s3_secret_key` | str | — | no | Assets bucket secret key. |
| `assets_s3_endpoint` | str | — | no | Assets bucket endpoint URL (a full `http://` URL for plain-HTTP endpoints; a bare host is treated as https — `geneva_examples/examples/video/ingest_external_refs.py:33-44`, mirrored as the worker-side fallback in `chunkers_uri.py:218`). |
| `assets_s3_region` | str | — | no | Assets bucket region. |
| `hf_token` | str | — | no | Hugging Face token: raises download rate limits, and is injected into chunker workers' env as `HF_TOKEN` when set (`geneva_examples/examples/video/chunk_openvid.py:116`). An empty string collapses to `None`. |

How the storage credentials are assembled: `Config.storage_options()`
(`geneva_examples/core/config.py:91-118`) builds the object-store options for the
connection with this precedence:

1. **Azure wins** when both `azure_account_name` and `azure_account_key` are set —
   the result is `{azure_storage_account_name, azure_storage_account_key}`, and
   the `s3_*` keys are ignored
   (`tests/test_config.py::test_azure_credentials_take_precedence_over_s3`).
2. Else **S3 requires all four** of `s3_access_key`/`s3_secret_key`/`s3_endpoint`/
   `s3_region`; a partial set yields `None`, never a partial dict. The S3 dict
   always adds `aws_s3_force_path_style: "true"` and `aws_allow_http:
   "true"|"false"`.
3. Else `None` — geneva connects without explicit storage options.

`Config` reads YAML only: no environment variable configures mode, credentials, or
the connection target. The environment variables the repo does read and set are
listed in
[docs/reference/environment-variables.md](../reference/environment-variables.md).

## The two credential sets

The config carries two independent credential sets, and they never fall back to
each other (`Config` docstring, `geneva_examples/core/config.py:56-65`):

- **Storage bucket** (`s3_*`, or `azure_account_*` on Azure): the connection's
  storage options — the token that reads and writes the `.lance` data files.
  Enterprise clients write Lance data fragments directly to object storage, which
  is why the driver needs this token at all. There is no Azure variant of the
  assets set; `azure_account_*` covers the storage bucket only.
- **Assets bucket** (`assets_s3_*`): a separate, typically bucket-scoped token
  used only by the external-refs video steps (`ingest-videos-external`,
  `chunk-videos-external`) to enumerate and stream raw videos. Workers receive it
  as `ASSETS_S3_*` environment variables via the manifest — see
  [docs/reference/environment-variables.md](../reference/environment-variables.md)
  and [docs/workflows/video.md](../workflows/video.md).

The independence is deliberate, but the two sets fail differently: a missing
assets credential is a hard error — the external-refs steps raise
`RuntimeError: missing video-bucket credentials ...`
(`geneva_examples/examples/video/ingest_external_refs.py:78-82`) — while a
missing or partial storage set is silent: `storage_options()` returns `None` and
geneva connects without it, so the failure surfaces later as an object-store
error on the first write. Per command, the `--video-*`
flags override the `assets_s3_*` block (see the generated flag reference,
[docs/reference/cli/video.md](../reference/cli/video.md#ingest-videos-external)).

## db_uri normalization

geneva reads any URI that is *not* `db://...` as an **on-disk** database path,
created relative to the working directory. In enterprise mode a bare name like
`smoke` would therefore silently create `./smoke/` on your machine while you
believe you are talking to the cluster — this accident is where stray
`./<name>/__manifest/` and `<name>___system$geneva_jobs/` directories at the repo
root come from (the marker patterns in `.gitignore` exist for exactly this).

`normalize_db_uri()` (`geneva_examples/core/config.py:145-173`) closes that trap:

| Input (enterprise mode) | Result |
| --- | --- |
| Bare name (`quickstart`) | Corrected to `db://quickstart`, logged at WARNING |
| Any URI containing `://` (`db://`, `s3://`, `gs://`, `az://`) | Passed through — geneva supports object storage directly |
| Explicit path (`/...`, `./...`, `../...`, `~...`) | Passed through — treated as a deliberate on-disk database |
| Blank / whitespace | Passed through |
| Anything in local mode | Passed through untouched (and then unused) |

For a deliberate on-disk scratch database, use an explicit path such as
`--db-uri ./.geneva/smoke` (`.geneva/` is gitignored).

## Object-store regions

With Cloudflare R2, set the region keys — `lancedb_region` and `s3_region`, plus
`assets_s3_region` when the assets bucket is also on R2 — to `us-east-1`. R2
accepts `us-east-1` as the SigV4 alias for its `auto` region; a bucket-location
region such as `enam` is rejected, and every request fails with HTTP 403
`SignatureDoesNotMatch`. This is a property of R2's S3-compatible API, not of any
particular deployment.

For AWS S3 and other S3-compatible stores, use the bucket's actual region. The
403 symptom and other connection failures are cataloged in
[docs/operations/troubleshooting.md](../operations/troubleshooting.md).

## Table names are not configuration

Table names are deliberately not config keys: each step declares its own table
flag default, so the target table is explicit per command
(`geneva_examples/core/config.py:17-19`). The tables the five examples create:

| Table | Steps that create/fill it |
| --- | --- |
| `images` | `ingest-images`, then `lightweight` / `embed` / `caption` |
| `videos` | `ingest-videos` / `ingest-videos-openvid` / `ingest-videos-external` |
| `video_clips` | the `chunk-videos*` steps (as a materialized view) and `seed-video-clips`; read by `frame-*` |
| `pdfs` | `ingest-pdfs`, then `chunk-pdfs` |
| `audio` | `ingest-audio`, then `synthesize-audio` / `transcribe-audio` / `export-audio` |
| `debug_demo` | `demo-errors` |

The authoritative per-step flag names and defaults (`--table-name`,
`--source-table`, `--clips-table`) are in the generated CLI reference —
start at [docs/reference/cli/index.md](../reference/cli/index.md). Column-level
schemas are in
[docs/reference/tables-and-schemas.md](../reference/tables-and-schemas.md).

## Templates

Start from the mode-specific template and copy it to `config.yaml`:

```bash
cp config-example-local.yaml config.yaml        # local mode (also fine: no file at all)
cp config-example-enterprise.yaml config.yaml   # enterprise mode — fill in the three required keys
```

`config.yaml` and the whole `config.*.yaml` family are gitignored so credentials
never land in git (`.gitignore`). Both templates document their keys with inline
comments; the `azure_account_*` keys are accepted whether or not your template
shows them. In enterprise mode, a missing `config.yaml` or a missing required key
fails with a `RuntimeError` that names the file and the missing keys
(`geneva_examples/core/config.py:202-215`).
