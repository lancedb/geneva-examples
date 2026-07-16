# Authoring tasks, UDFs & params in geneva-examples

A practical map of **what lives where** and **how to add your own** ingest/backfill
tasks and UDFs — written for the case where you need to run *hundreds* of UDFs and
jobs against a Geneva/LanceDB cluster.

The **external-storage reference** video pipeline is used throughout as the worked
example:
- ingest task → [`examples/video/ingest_external_refs.py`](geneva_examples/examples/video/ingest_external_refs.py)
- chunk task → [`examples/video/chunk_external_video.py`](geneva_examples/examples/video/chunk_external_video.py)
- UDTF (chunker) → [`examples/video/chunkers_uri.py`](geneva_examples/examples/video/chunkers_uri.py)

---

## 1. Mental model

Everything the CLIs and the TUI expose is generated from **one spec per step**.

- An **`Example`** is one end-to-end pipeline for a modality (e.g. `video`). It owns an
  ordered list of **`Step`s**.
- A **`Step`** is one CLI command. It points at a **`run(cfg, *, ...)`** function and
  declares its tunable **`Param`s** (auto-derived from the function signature).
- A **UDF/UDTF** is a self-contained *factory function* decorated with `@geneva.udf`
  or `@geneva.chunker`. A `run()` builds one and hands it to `create_udtf_view` /
  `add_columns`, then calls `refresh` / `backfill`.

The spec is the **single source of truth**: the `uv run <name>` CLIs *and* the Textual
TUI both render from it, so a step's description/params are defined exactly once.

```
Example (video)
 └── Step "chunk-videos-external"
      ├── run = chunk_external_video.run       # does the work
      ├── params = params_from_signature(run, help=…)   # → CLI options + TUI fields
      └── (registered in cli.py + pyproject → `uv run chunk-videos-external`)
```

---

## 2. Where things live

| Path | Responsibility |
|---|---|
| `geneva_examples/core/spec.py` | The framework: `Example`, `Step`, `Param`, `params_from_signature`, `build_command`, `COMMON_HELP`. **Read this first.** |
| `geneva_examples/core/config.py` | `Config` + `load_config` — parses `config.yaml` (mode, creds, `db_uri`, `storage_options`). |
| `geneva_examples/core/common.py` | `connect`, `build_manifest`, `runtime_session`, `resolve_resources`, `local_concurrency`, `format_sample`, `setup_logging`. Shared plumbing every task uses. |
| `geneva_examples/examples/<modality>/` | One package per modality (`video`, `images`, `pdf`). |
| `geneva_examples/examples/<modality>/__init__.py` | Defines the `Step`s and the `Example` for that modality. **Registration point #1.** |
| `geneva_examples/examples/<modality>/<task>.py` | A task = a module with a `run(cfg, *, ...)` function. |
| `geneva_examples/examples/video/chunkers*.py`, `.../udfs/` | The UDF/UDTF *factories*. |
| `geneva_examples/examples/cli.py` | `build_command(EXAMPLE, STEP)` per command. **Registration point #2.** |
| `pyproject.toml` `[project.scripts]` | Maps `uv run <name>` → `cli.py:<command>`. **Registration point #3.** |
| `geneva_examples/ops/` | Cross-cutting ops CLIs: `jobs` (list/cancel), `cleanup`, `stats`. |
| `geneva_examples/tui/app.py` | `uv run tui` — interactive runner, renders from the same specs. |

---

## 3. Add a new task (the 3-file + 3-edit pattern)

### Step A — write the `run()` module

Create `geneva_examples/examples/<modality>/<task>.py` with a single entry point:

```python
def run(
    cfg: Config,
    *,
    table_name: str = "videos",
    limit: int = 100,
    # ...your params; the type + default here drive the CLI option...
) -> None:
    """One-line summary (becomes part of the step description)."""
    conn = connect(cfg)              # from core.common — honors mode + storage_options
    # ...do the work: read/enumerate, create_table / create_udtf_view, refresh...
```

Rules that make it "just work":
- **First arg is `cfg: Config`**; everything else is **keyword-only** (`*,`) with a
  type annotation and a default. That's what `params_from_signature` turns into CLI
  options and TUI fields.
- Keep the module **import-cheap**: nest heavy imports (`geneva`, `torch`, `av`, …)
  *inside* `run()` (and inside UDF closures), so importing the spec registry to list
  commands never pulls in the ML stack.
- Use `connect(cfg)` for the connection, `retry_io(...)` for table writes, and
  `runtime_session(conn, cfg)` around `refresh()` (a no-op in enterprise, provisions
  local Ray in local mode).

### Step B — register the `Step` (3 existing-file edits)

1. **`examples/<modality>/__init__.py`** — import the module and add a `Step`, then add
   it to `EXAMPLE.steps`:
   ```python
   from geneva_examples.examples.video import ..., my_task

   MY_TASK = Step(
       key="my-task",                       # the CLI subcommand name
       title="Human title",
       description="What it does (shown in --help and the TUI).",
       run=my_task.run,
       requires="run ingest-… first",       # optional UI hint
       params=params_from_signature(
           my_task.run,
           help=COMMON_HELP | {"limit": "Max rows…"},   # per-param help
       ),
   )
   # add MY_TASK to EXAMPLE.steps=(...)
   ```
2. **`examples/cli.py`** — one line:
   ```python
   my_task_cmd = build_command(video.EXAMPLE, video.MY_TASK)
   ```
3. **`pyproject.toml` `[project.scripts]`** — one line:
   ```toml
   my-task = "geneva_examples.examples.cli:my_task_cmd"
   ```

### Step C — regenerate entry points

```bash
uv sync            # regenerates the console scripts in .venv/bin
uv run my-task --help
```

Every command automatically gets the common options `--config / --mode / --db-uri /
--log-level` for free (see `build_command`); you only declare the step-specific params.

---

## 4. Params: how CLI options & TUI fields are generated

`params_from_signature(run, help=…, choices=…, bounds=…)` (in `core/spec.py`) reads the
`run()` signature and emits one `Param` per keyword arg:

- **name** → `--kebab-case` flag and the `run()` kwarg.
- **type** from the annotation: `str | int | float | bool` (and `X | None`
  unwraps to `X`). `bool` becomes a `--flag/--no-flag` pair.
- **default** from the signature default (shown as `[default: …]`).
- **help** from the `help=` dict; unknown params fall back to the humanized name. Reuse
  `COMMON_HELP` for recurring params (`table_name`, `concurrency`, `num_cpus`, …) and
  merge overrides with `COMMON_HELP | {...}`.
- **choices** (`click.Choice`) and **bounds** (`IntRange`/`FloatRange`) via the optional
  `choices=` / `bounds=` args.

So to add a knob to any task: **add a keyword arg to `run()`** and (optionally) a help
line. Nothing else.

---

## 5. UDFs & UDTFs (the factory pattern)

UDF/UDTF factories live beside their tasks (e.g. `examples/video/chunkers.py`,
`chunkers_uri.py`, `examples/video/udfs/…` upstream). A factory returns a decorated
callable:

```python
def my_udtf(*, manifest, num_cpus=1.0, memory_bytes=2 * 1024**3):
    import geneva, pyarrow as pa
    output_schema = pa.schema([("out_col", pa.large_binary()), ...])

    @geneva.chunker(                       # or @geneva.udf for 1:1 columns
        output_schema=output_schema,
        input_columns=["in_col"],
        inherit_input_columns=False,       # don't copy inputs onto output rows
        num_cpus=num_cpus, num_gpus=0.0, memory=memory_bytes,
        version=uuid.uuid4().hex,
        manifest=manifest,
    )
    def _fn(in_col):
        # RUNS ON THE WORKER. All imports + helpers MUST be nested here — this
        # module is NOT importable on the remote runtime; only the manifest's
        # pip packages are. Marshalled by value.
        import av  # etc.
        ...
        yield {"out_col": ...}             # chunker: yield 1..N rows per input row
    return _fn
```

Key rules:
- **Self-contained closure.** Nest every import and helper inside the decorated
  function. A reference to a module-level symbol will `NameError` on the worker.
- **Declare the output type** in `output_schema` (chunker) or `data_type` (udf). For a
  blob column, add `field_metadata={"lance-encoding:blob": "true"}` (legacy, file
  version ≤ 2.1) or use `lance.blob_field(...)` (blob-v2, file version ≥ 2.2).
- **Resources** (`num_cpus` / `num_gpus` / `memory`) become each actor's Ray demand;
  total cluster demand ≈ `concurrency × per-actor`. The KubeRay autoscaler provisions
  workers up to `worker_max_replicas` to satisfy pending actors.
- **`input_columns`** are fetched and passed positionally (param names need not match).
  `inherit_input_columns=False` keeps large inputs off the output rows; columns
  *selected but not consumed* (e.g. `video_id`) are carried onto every output row.

Three byte-source variants already exist as references:
`chunk_video_udtf` (inline bytes), `chunk_blob_video_udtf` (Lance blob via `take_blobs`),
and `chunk_uri_video_udtf` (opens an S3 URI on the worker).

---

## 6. Connections, config & credentials

- **`config.yaml`** (see `config-example-*.yaml`) drives `Config`: `mode`
  (`local`/`enterprise`), `db_uri`, LanceDB `api_key`/`region`/`geneva_host`, and the
  primary object-store creds (`cfg.storage_options()`). `connect(cfg)` uses these.
- **Enterprise writes are client-side**: `create_table` writes the `.lance` data files
  from the client using the connection's `storage_options`; the query-node registers the
  namespace. So the *client's* pylance version governs on-disk encoding.
- **Extra object-store creds for the workers** (e.g. a video bucket with a *different,
  bucket-scoped* token than the LanceDB bucket): the connection's `storage_options`
  won't reach it. Inject them via the **manifest `env_vars`**, and read them from
  `os.environ` inside the UDF:
  ```python
  manifest = (GenevaManifest.create_pip(name)
              .pip([*VIDEO_RUNTIME_PIP])
              .env_vars({"VIDEO_S3_ACCESS_KEY": ..., "VIDEO_S3_SECRET_KEY": ...,
                         "VIDEO_S3_ENDPOINT": ...})
              .build())
  # …and in the UDF: pyarrow.fs.S3FileSystem(access_key=os.environ["VIDEO_S3_ACCESS_KEY"], …)
  ```
  ⚠️ **Security**: env_vars are stored in the manifest/job record and shipped to workers
  as plaintext. For production use a k8s Secret / secret store / workload identity, not
  literal secrets in the manifest.
- **Local vs enterprise** in a task: `if cfg.is_local:` set the env in-process and pass
  `manifest=None`; else build the manifest with `env_vars`. See `chunk_external_video.run`.

---

## 7. Running at scale (hundreds of UDFs / jobs)

**Fan-out knobs** on `refresh()` / the chunker decorator:

| Knob | Where | Effect |
|---|---|---|
| `source_task_size` | `refresh(...)` | Source rows per task. Default **1024**; set **1** to fan out one input per task (essential for heavy per-row work like video decode). Work items = `ceil(rows / source_task_size)`. |
| `concurrency` | `refresh(...)` | Cap on parallel actors. `num_actors = min(work_items, concurrency)`. Default 8. |
| `num_cpus` / `num_gpus` / `memory` | chunker/udf decorator | Per-actor Ray demand → drives autoscaling. |
| `worker_max_replicas` | KubeRay cluster (infra) | Ceiling on worker pods the autoscaler will add (default often 10 — raise for real fan-out). |
| `max_rows_per_fragment` / `checkpoint_size` | `refresh(...)` | Output fragment size / checkpoint cadence; bounds actor memory. |

**Observability when detached** (e.g. `backfill_async`): the refresh runs in a **driver
Job pod on the cluster**, not your client — your client only streams progress bars.

- **Status/progress, from anywhere with creds**: capture the returned `job_id`, then
  `conn.get_job(job_id)` or `conn.list_jobs(table_name=…, status=…)` (see `ops/jobs.py`).
  These read the durable `geneva_jobs` system table (status / events / metrics).
- **Full driver + per-task logs**: the driver Job pod (`kubectl -n lancedb logs
  <…refresh…-pod> -f`), the worker pods / Ray dashboard (`raycluster-head-svc:8265`), or
  the `mf` CLI. Central sinks (Grafana/Oodle) receive them only if the cluster's OTEL
  collector (`LANCEDB_OTEL_COLLECTOR_URL`) is wired.
- **Ops CLIs**: `uv run jobs` (list/cancel), `uv run cleanup`, `uv run stats`.

**Batching many jobs**: each Step is idempotent-ish and parameterized, so a driver script
can loop over inputs/params and call the `run()` functions (or `uv run <cmd>`) directly —
they don't have to go through the TUI. Give each a distinct `job_id` for traceability.

---

## 8. Add a whole new modality (a new `Example`)

1. Create `geneva_examples/examples/<modality>/` with `__init__.py` defining `Step`s and
   an `EXAMPLE = Example(name=…, modality=…, steps=(…))`.
2. Import it in `examples/cli.py` and add `build_command(<mod>.EXAMPLE, <mod>.<STEP>)`
   lines.
3. Add `[project.scripts]` entries, then `uv sync`.

---

## 9. Checklists

**New task**
- [ ] `examples/<modality>/<task>.py` with `run(cfg, *, …)` (keyword-only, typed, defaulted; heavy imports nested)
- [ ] `Step` + added to `EXAMPLE.steps` in `__init__.py`
- [ ] `build_command(...)` in `cli.py`
- [ ] `[project.scripts]` in `pyproject.toml`
- [ ] `uv sync` → `uv run <name> --help`

**New UDF/UDTF**
- [ ] Factory returns a `@geneva.udf` / `@geneva.chunker` callable
- [ ] All imports + helpers nested in the closure
- [ ] `output_schema` / `data_type` set (blob metadata if bytes)
- [ ] Resources (`num_cpus`/`num_gpus`/`memory`) sized to the work
- [ ] Any external creds injected via manifest `env_vars` and read from `os.environ`
