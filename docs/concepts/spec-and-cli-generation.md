# The spec and CLI generation

> Part of the geneva-examples docs — index: [docs/README.md](../README.md).

## Contents

- [The one big idea](#the-one-big-idea)
- [Anatomy of the spec](#anatomy-of-the-spec)
- [From spec to console script](#from-spec-to-console-script)
- [The four common options](#the-four-common-options)
- [Param-to-flag mapping rules](#param-to-flag-mapping-rules)
- [The TUI side](#the-tui-side)

## The one big idea

You never write a CLI by hand. Each step is one plain function `run(cfg, *, ...)`;
a `Step` spec describes it once (command name, help text, params); and the
framework generates every front-end from that spec: the `uv run <step>` console
script, the TUI form, and the generated CLI reference pages under
`docs/reference/cli/`. A step's description and parameters therefore exist exactly
once. The spec objects live in `geneva_examples/core/spec.py`:

| Object | What it is | Where instances are defined |
|---|---|---|
| `Param` | one tunable parameter — renders to a CLI option and a TUI form field | each example's `__init__.py` (hand-written or derived) |
| `Step` | one command: title, help text, which `run()` to call, its params | each example's `__init__.py` |
| `Example` | one pipeline: an ordered tuple of steps for one modality | bottom of each example's `__init__.py` |

## Anatomy of the spec

All three are frozen dataclasses in `geneva_examples/core/spec.py`. Fields marked
"required" have no default.

`Param` (`geneva_examples/core/spec.py:31`):

| Field | Type | Default | Meaning |
|---|---|---|---|
| `name` | `str` | required | snake_case; maps to the `--kebab-case` flag (the `cli_flag` property) and the `run()` kwarg |
| `type` | `type` | required | one of `str`, `int`, `float`, `bool` |
| `default` | `Any` | required | the CLI and TUI default, rendered into `--help` (`show_default=True`) |
| `help` | `str` | required | option help text |
| `choices` | `tuple[str, ...] \| None` | `None` | restricts values via `click.Choice` |
| `min`, `max` | `float \| None` | `None` | numeric bounds via `click.IntRange` / `click.FloatRange` |

`Step` (`geneva_examples/core/spec.py:48`):

| Field | Type | Default | Meaning |
|---|---|---|---|
| `key` | `str` | required | the command name: `uv run <key>` |
| `title` | `str` | required | short human title (TUI tree label, generated reference) |
| `description` | `str` | required | the `--help` text and the TUI markdown pane — a user-facing docs surface |
| `run` | `Callable` | required | `run(cfg, **params)`; does the work |
| `params` | `tuple[Param, ...]` | `()` | the step's tunable params |
| `gpu` | `bool` | `False` | UI hint only: the step runs a model (CPU-only in local mode). It does not affect scheduling — resource requests live on the UDF itself |
| `requires` | `str` | `""` | UI hint naming a prerequisite, e.g. "run ingest-images first" |
| `default_mode` | `str \| None` | `None` | default for the generated `--mode` option. `None` keeps config-driven mode resolution; `demo-errors` is the only step that pins `"local"` (`geneva_examples/examples/debugging/__init__.py`) |

`Example` (`geneva_examples/core/spec.py:141`):

| Field | Type | Meaning |
|---|---|---|
| `name` | `str` | registry key; `get_example(name)` looks it up |
| `title` | `str` | human title |
| `description` | `str` | markdown shown in the TUI and the generated reference |
| `modality` | `str` | free-form UI hint. Live values: `image`, `video`, `pdf`, `audio`, `demo` |
| `steps` | `tuple[Step, ...]` | the ordered pipeline steps; `Example.step(key)` raises `KeyError` for unknown keys |

`COMMON_HELP` (`geneva_examples/core/spec.py:66`) is a shared dictionary of help
strings for parameters that recur across steps (`table_name`, `concurrency`,
`num_cpus`, …). Per-step specs merge overrides on top with `COMMON_HELP | {...}`.

## From spec to console script

Typing `uv run embed` reaches your `run()` through five links:

1. **The example package defines the spec.** `examples/<example>/__init__.py`
   builds each `Step`. Params come from one of two places:
   - **Derived** — `params_from_signature(run, help=COMMON_HELP | {...})` reads the
     `run(cfg, *, ...)` signature and emits one `Param` per keyword-only argument.
     Name, type, and default come from the signature; the parameter named `cfg` and
     any `*args`/`**kwargs` are skipped; missing help falls back to the humanized
     name; optional `choices=` and `bounds=` dicts add constraints
     (`geneva_examples/core/spec.py:106`). The video, pdf, and audio examples use
     this, so adding a keyword argument to their `run()` adds a `--flag` and a TUI
     field with no other edit.
   - **Hand-written** — a literal `Param(...)` tuple, used when the spec needs
     bounds or help the signature alone cannot carry.
     `geneva_examples/examples/images/__init__.py` and
     `geneva_examples/examples/debugging/__init__.py` hand-write every `Param` and
     never call `params_from_signature`.
2. **The registry aggregates.** `geneva_examples/examples/__init__.py` exports
   `EXAMPLES = (IMAGES, VIDEO, PDF, AUDIO, DEBUGGING)` plus `all_examples()`,
   `get_example()`, and `iter_steps()`. It must stay import-cheap (no torch/geneva
   at import time; enforced by `tests/test_registry.py`).
3. **`build_command` turns each step into a `click.Command`.**
   `geneva_examples/examples/cli.py` holds one
   `<symbol> = build_command(<pkg>.EXAMPLE, <pkg>.<STEP>)` line per step.
4. **`[project.scripts]` names the command.** `pyproject.toml` maps each command
   name to its `cli.py` symbol, e.g. `embed = "geneva_examples.examples.cli:embed"`.
5. **`uv sync` materializes it**, regenerating the console scripts in `.venv/bin`
   so `uv run <key>` resolves.

`build_command(example, step)` (`geneva_examples/core/spec.py:213`) constructs a
`click.Command(name=step.key, help=step.description.strip(), ...)` carrying the
four common options, then layers `step.params` on top via `_param_option` — click
appends to `cmd.params`, so the common options come first. The command's callback
calls `resolve_config(...)` and then `step.run(cfg, **params)`. Two facts worth
stating so you don't over-infer:

- **`build_command` consumes the already-built `step.params`; it never calls
  `params_from_signature`.** Whether a step's params are derived or hand-written is
  decided in the example package at spec-definition time.
- **The `example` argument is unused in the body.** The generated command derives
  everything from `step`; passing a different `Example` changes nothing about the
  command.

The generated CLI reference is a third render of the same spec:
`geneva_examples/docs_gen/render.py` instantiates `build_command(example, step)`
and walks `cmd.params`, so its flag tables match `--help` exactly. Regenerate with
`make docs`; start at [docs/reference/cli/index.md](../reference/cli/index.md).

## The four common options

Every generated command accepts the same four options ahead of its own params
(`geneva_examples/core/spec.py:226`):

| Option | Feeds | Behavior |
|---|---|---|
| `--config PATH` | `load_config` | Path to the YAML config. Unset resolves to `./config.yaml` relative to the current working directory |
| `--mode [local\|enterprise]` | mode override | Defaults to `step.default_mode`, which is unset for every step except `demo-errors` — that step pins `local` (pass `--mode enterprise` to opt out). An unset default is what keeps mode resolution config-driven (`resolve_mode` in `geneva_examples/core/config.py`) |
| `--db-uri TEXT` | db_uri override | Overrides the config `db_uri` (enterprise mode); a blank value is ignored |
| `--log-level TEXT` | `setup_logging` | `DEBUG` restores full geneva/ray/lance logging and worker-log forwarding |

The rendered defaults appear in the generated reference, e.g.
[docs/reference/cli/images.md](../reference/cli/images.md#common-options-every-command).

All four funnel into `resolve_config()` (`geneva_examples/core/spec.py:161`), the
single choke point every CLI and the TUI pass through before Ray can start. It does
three things, in order:

1. `os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")` — otherwise Ray's
   `uv run` runtime-env integration packages the entire working directory (HF
   caches, `local_db/`, …) and uploads it, blowing past Ray's 512 MiB `working_dir`
   limit. See
   [docs/reference/environment-variables.md](../reference/environment-variables.md).
2. `setup_logging(log_level)` — which also sets `LANCE_LOG=warn` unless the level
   is `DEBUG` (`geneva_examples/core/common.py`).
3. `load_config(config, mode_override=mode, db_uri_override=db_uri)` — the mode and
   `db_uri` precedence rules are documented in
   [docs/getting-started/configuration.md](../getting-started/configuration.md).

## Param-to-flag mapping rules

`_param_option` (`geneva_examples/core/spec.py:184`) maps each `Param` to a
`click.option`. The rules are checked in this order — earlier rules win:

| Order | Condition | Rendered option |
|---|---|---|
| 1 | `type is bool` | paired flag `--name/--no-name`. Checked first, so a bool `Param` with `choices` set ignores its choices |
| 2 | `choices is not None` | `click.Choice(choices)` |
| 3 | `type is int` | `click.IntRange(min, max)` if either bound is set, else plain `int` |
| 4 | `type is float` | `click.FloatRange(min, max)` if either bound is set, else plain `float` |
| 5 | anything else | `str` |

Every option renders with `show_default=True` and the `Param.help` text. Flag names
are the snake_case `Param.name` with underscores replaced by hyphens
(`Param.cli_flag`).

One note for derived params: the example modules use
`from __future__ import annotations`, so `run()` annotations arrive as strings.
`_annotation_type` (`geneva_examples/core/spec.py:95`) strips `| None` /
`Optional`, returns the first of `str`/`int`/`float`/`bool` it finds in the text,
and falls back to `str`.

## The TUI side

`uv run tui` renders the same registry. The Examples tree lists every example and
step; selecting a step shows `Step.description` as markdown plus a form with one
field per `Param`: `bool` params render as switches, `choices` as select widgets,
everything else as text inputs (`geneva_examples/tui/app.py:364`). Two behaviors
worth knowing:

- **Blank means default.** An empty form field falls back to `Param.default`
  (`geneva_examples/tui/forms.py:19`), so an untouched form runs the step exactly
  as the bare CLI command would.
- **Run launches the step's own console script as a subprocess**, passing the form
  values as the same `--flags` the CLI takes (`_build_argv` in
  `geneva_examples/tui/app.py`). A subprocess rather than an in-process thread
  because Ray needs a real stdout file descriptor.

The CLI and the TUI therefore cannot drift: both are projections of the same
`Step`. Day-to-day TUI usage is covered in
[docs/workflows/tui.md](../workflows/tui.md).
