"""Declarative spec for self-contained example pipelines.

An :class:`Example` is one end-to-end pipeline (e.g. the image feature workflow).
It owns an ordered list of :class:`Step`s (ingest, then feature stages), and each
step declares its tunable :class:`Param`s and a ``run(cfg, **params)`` callable.

This spec is the **single source of truth**: both the generated ``uv run <name>``
CLIs (:func:`build_command`) and the Textual TUI render from it, so a step's
description and parameters are defined exactly once.

Keep this module import-cheap. The ``run`` callables and UDF factories nest their
heavy imports (torch, geneva, …) inside their bodies, so importing the registry to
list/describe examples never pulls in the ML stack.
"""

from __future__ import annotations

import inspect
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click

from geneva_examples.core.common import setup_logging
from geneva_examples.core.config import VALID_MODES, Config, load_config


@dataclass(frozen=True)
class Param:
    """One tunable parameter: renders to a CLI option and a TUI form field."""

    name: str  # snake_case; maps to --kebab-case and the run() kwarg
    type: type  # str | int | float | bool
    default: Any
    help: str
    choices: tuple[str, ...] | None = None
    min: float | None = None
    max: float | None = None

    @property
    def cli_flag(self) -> str:
        return "--" + self.name.replace("_", "-")


@dataclass(frozen=True)
class Step:
    """One stage of a pipeline. ``run(cfg, **params)`` does the work."""

    key: str
    title: str
    description: str
    run: Callable[..., None]
    params: tuple[Param, ...] = ()
    gpu: bool = False  # UI hint: runs a model (CPU-only in local mode)
    requires: str = ""  # UI hint, e.g. "run the ingest step first"


# Help text for parameters that recur across many steps. Per-step specs merge
# their own overrides on top (``COMMON_HELP | {...}``).
COMMON_HELP: dict[str, str] = {
    "table_name": "Table to operate on.",
    "input_column": "Column of encoded images.",
    "output_column": "Output column name.",
    "batch_size": "DataLoader batch size (auto-shrunk locally).",
    "num_workers": "DataLoader worker processes (0 locally).",
    "num_cpus": "CPUs per task (capped to the machine locally).",
    "num_gpus": "GPUs per task (forced to 0 locally).",
    "memory_gib": "Memory (GiB) per task (geneva caps <2).",
    "checkpoint_size": "Rows per checkpoint / UDF __call__.",
    "task_size": "Rows per read task.",
    "concurrency": "Backfill/refresh concurrency (1 locally).",
    "backfill_timeout_min": "Per-backfill timeout (minutes).",
    "flush_interval_s": "Checkpoint flush interval (seconds).",
    "schema_wait_attempts": "Schema-visibility attempts.",
    "schema_wait_sleep_s": "Seconds between schema checks.",
    "overwrite": "Drop the table first if it already exists.",
    "frag_size": "Rows per record batch.",
    "table_write_retries": "Retries for create/add ops.",
    "table_write_retry_sleep_s": "Base sleep (s) between table-write retries.",
    "use_cpu_only_pool": "Use the CPU-only worker pool (enterprise).",
    "source_table": "Source table name.",
    "clips_table": "Output clips table name.",
    "chunk_seconds": "Chunk length in seconds.",
}

_TYPE_NAMES = {"str": str, "int": int, "float": float, "bool": bool}


def _annotation_type(annotation: Any) -> type:
    """Map a run() parameter annotation (possibly a string) to a scalar type."""
    if isinstance(annotation, type):
        return annotation
    text = str(annotation).replace("| None", "").replace("Optional", "").strip(" []")
    for token in text.replace("|", " ").split():
        if token in _TYPE_NAMES:
            return _TYPE_NAMES[token]
    return str


def params_from_signature(
    run: Callable[..., None],
    *,
    help: dict[str, str] | None = None,
    choices: dict[str, tuple[str, ...]] | None = None,
    bounds: dict[str, tuple[float | None, float | None]] | None = None,
) -> tuple[Param, ...]:
    """Derive ``Param``s from a ``run(cfg, *, ...)`` signature.

    Name/type/default come from the signature; ``help`` supplies descriptions
    (missing ones fall back to the humanized name). This keeps a step's params in
    lockstep with its ``run`` function without hand-restating every default.
    """
    help = help or {}
    choices = choices or {}
    bounds = bounds or {}
    out: list[Param] = []
    for name, p in inspect.signature(run).parameters.items():
        if name == "cfg" or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        lo, hi = bounds.get(name, (None, None))
        out.append(
            Param(
                name=name,
                type=_annotation_type(p.annotation),
                default=p.default,
                help=help.get(name, name.replace("_", " ")),
                choices=choices.get(name),
                min=lo,
                max=hi,
            )
        )
    return tuple(out)


@dataclass(frozen=True)
class Example:
    """A self-contained example pipeline: a series of steps over one modality."""

    name: str
    title: str
    description: str
    modality: str  # image | video | pdf
    steps: tuple[Step, ...] = field(default_factory=tuple)

    def step(self, key: str) -> Step:
        for s in self.steps:
            if s.key == key:
                return s
        raise KeyError(f"{self.name} has no step {key!r}")


# --- shared parameter resolution --------------------------------------------

_COMMON = ("config", "mode", "db_uri", "log_level")


def resolve_config(
    *,
    config: Path | None,
    mode: str | None,
    db_uri: str | None,
    log_level: str,
) -> Config:
    """Apply the common controls: load config, override db_uri, set up logging.

    Also disables Ray's ``uv run`` runtime-env integration, which otherwise
    packages the whole working directory (HF caches, local_db, …) and uploads it
    to the Ray cluster — blowing past Ray's 512 MiB working_dir limit. Set here,
    the single funnel every CLI and the TUI pass through before Ray starts, so no
    individual step can forget it.
    """
    os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")
    setup_logging(log_level)
    cfg = load_config(config, mode_override=mode)
    if db_uri:
        cfg.db_uri = db_uri
    return cfg


# --- CLI generation ---------------------------------------------------------


def _param_option(param: Param):
    """Build the ``click.option`` decorator for a :class:`Param`."""
    kwargs: dict[str, Any] = {
        "default": param.default,
        "show_default": True,
        "help": param.help,
    }
    if param.type is bool:
        flag = param.name.replace("_", "-")
        return click.option(f"--{flag}/--no-{flag}", param.name, **kwargs)
    if param.choices is not None:
        kwargs["type"] = click.Choice(param.choices)
    elif param.type is int:
        kwargs["type"] = (
            click.IntRange(param.min, param.max)
            if (param.min is not None or param.max is not None)
            else int
        )
    elif param.type is float:
        kwargs["type"] = (
            click.FloatRange(param.min, param.max)
            if (param.min is not None or param.max is not None)
            else float
        )
    else:
        kwargs["type"] = str
    return click.option(param.cli_flag, param.name, **kwargs)


def build_command(example: Example, step: Step) -> click.Command:
    """Generate a ``click.Command`` for one step (used as a console script)."""

    def _callback(config, mode, db_uri, log_level, **params):
        cfg = resolve_config(
            config=config, mode=mode, db_uri=db_uri, log_level=log_level
        )
        step.run(cfg, **params)

    cmd = click.Command(
        name=step.key,
        callback=_callback,
        help=step.description.strip(),
        params=[
            click.Option(
                ["--config"],
                type=click.Path(dir_okay=False, path_type=Path),
                default=None,
                help="Path to config.yaml (default ./config.yaml).",
            ),
            click.Option(
                ["--mode"],
                type=click.Choice(VALID_MODES),
                default=None,
                help="Connection mode: 'local' or 'enterprise'.",
            ),
            click.Option(
                ["--db-uri", "db_uri"],
                default=None,
                help="Override the config db_uri (enterprise mode).",
            ),
            click.Option(
                ["--log-level"],
                default="INFO",
                show_default=True,
                help="Logging level.",
            ),
        ],
    )
    # Layer the step's own params on top of the common options.
    for param in step.params:
        cmd = _param_option(param)(cmd)
    return cmd
