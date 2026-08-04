"""Render ``docs/reference/worker-runtime-pins.md`` from the manifest modules.

Each remote-worker manifest module defines a ``*_RUNTIME_PIP`` list built from
module-level ``*_PACKAGE_SPEC`` constants: ``geneva``/``lancedb``/``pylance``
resolve through :func:`geneva_examples.core.package_specs.package_spec` (so
they track the installed client versions), while the rest are exact-pinned
with an ``os.environ.get`` override. This module imports each manifest module
(they are import-cheap by design — heavy imports nest inside the UDF factory
bodies), regex-scans its *source* to attribute each ``*_PACKAGE_SPEC``
definition to the file that owns it, and renders the result as tables.

Because the ``package_spec`` values embed installed versions, a cluster-pin
bump in ``pyproject.toml`` makes ``make docs-check`` fail until ``make docs``
is re-run — the docs and the lockfile cannot disagree silently.
"""

from __future__ import annotations

import importlib
import inspect
import os
import re
from dataclasses import dataclass

from geneva_examples.core.package_specs import _default_env_var

# (module, RUNTIME_PIP attribute). Registry order of the owning examples.
MANIFEST_MODULES: tuple[tuple[str, str], ...] = (
    ("geneva_examples.examples.images.imageinfo", "IMAGEINFO_RUNTIME_PIP"),
    ("geneva_examples.examples._shared.clip", "CLIP_RUNTIME_PIP"),
    ("geneva_examples.examples._shared.blip", "BLIP_RUNTIME_PIP"),
    ("geneva_examples.examples.video.chunkers", "VIDEO_RUNTIME_PIP"),
    ("geneva_examples.examples.video.openpose", "OPENPOSE_RUNTIME_PIP"),
    ("geneva_examples.examples.video.seed", "BASE_RUNTIME_PIP"),
    ("geneva_examples.examples.pdf.document", "PDF_RUNTIME_PIP"),
    ("geneva_examples.examples.audio.tts", "MMS_TTS_RUNTIME_PIP"),
    ("geneva_examples.examples.audio.transcribe", "WHISPER_RUNTIME_PIP"),
    ("geneva_examples.examples.debugging.faulty", "FAULTY_RUNTIME_PIP"),
)

# Packages whose *_PACKAGE_SPEC env-var name deviates from the
# _default_env_var convention. Pinned to source by a test.
DEVIATIONS: tuple[tuple[str, str, str, str], ...] = (
    (
        "open-clip-torch",
        "OPEN_CLIP_TORCH_PACKAGE_SPEC",
        "OPEN_CLIP_PACKAGE_SPEC",
        "geneva_examples/examples/_shared/clip.py",
    ),
)

# Definition sites in a manifest module's source: either an explicit
# os.environ.get("<ENV>", "<default>") or a package_spec("<dist>") call.
_ENV_DEF_RE = re.compile(
    r"^(?P<attr>[A-Z0-9_]+_PACKAGE_SPEC) = os\.environ\.get\(\s*"
    r"\n?\s*\"(?P<env>[A-Z0-9_]+)\"",
    re.MULTILINE,
)
_SPEC_DEF_RE = re.compile(
    r"^(?P<attr>[A-Z0-9_]+_PACKAGE_SPEC) = package_spec\(\"(?P<dist>[^\"]+)\"\)",
    re.MULTILINE,
)


@dataclass(frozen=True)
class SpecVar:
    """One ``*_PACKAGE_SPEC`` definition: where it lives and what it resolves to."""

    env_var: str  # the environment variable that overrides it
    module_path: str  # repo-relative path of the defining module
    value: str  # the currently-resolved spec string
    tracks_installed: bool  # True when resolved via package_spec()


def assert_clean_spec_env() -> None:
    """Refuse to render with ambient pin overrides in the environment.

    A developer's local ``PYARROW_PACKAGE_SPEC`` (or ``MMS_TTS_MODEL_ID``)
    would silently bake into the committed page as "the default". A hard,
    named refusal is deterministic and honest; CI runs with a clean env.
    """
    offending = sorted(
        key
        for key in os.environ
        if key.endswith("_PACKAGE_SPEC") or key == "MMS_TTS_MODEL_ID"
    )
    if offending:
        raise RuntimeError(
            "refusing to generate docs with pin overrides set in the "
            f"environment: {', '.join(offending)} — unset them and re-run "
            "`make docs`"
        )


def _module_repo_path(module_name: str) -> str:
    return module_name.replace(".", "/") + ".py"


def collect() -> tuple[list[tuple[str, str, list[str]]], list[SpecVar]]:
    """Gather (manifest rows, spec-var definitions) from the manifest modules."""
    manifests: list[tuple[str, str, list[str]]] = []
    spec_vars: list[SpecVar] = []
    for module_name, pip_attr in MANIFEST_MODULES:
        module = importlib.import_module(module_name)
        module_path = _module_repo_path(module_name)
        manifests.append((pip_attr, module_path, list(getattr(module, pip_attr))))
        source = inspect.getsource(module)
        spec_vars.extend(
            SpecVar(
                env_var=match["env"],
                module_path=module_path,
                value=str(getattr(module, match["attr"])),
                tracks_installed=False,
            )
            for match in _ENV_DEF_RE.finditer(source)
        )
        spec_vars.extend(
            SpecVar(
                env_var=_default_env_var(match["dist"]),
                module_path=module_path,
                value=str(getattr(module, match["attr"])),
                tracks_installed=True,
            )
            for match in _SPEC_DEF_RE.finditer(source)
        )
    return manifests, spec_vars


def render_pins_page() -> str:
    """The full worker-runtime-pins reference page."""
    from geneva_examples.docs_gen.render import GENERATED_MARKER, escape_cell

    assert_clean_spec_env()
    manifests, spec_vars = collect()

    lines = [
        GENERATED_MARKER,
        "# Worker runtime pins and `*_PACKAGE_SPEC` overrides",
        "",
        "> **Generated file — do not edit.** Values are read from the installed",
        "> environment (`uv.lock`) and the manifest modules; regenerate with",
        "> `make docs` after any pin change.",
        "> Docs index: [`docs/README.md`](../README.md).",
        "",
        "Each remote-worker manifest installs the pip specs below (enterprise",
        "mode only; local mode installs nothing). `geneva`, `lancedb`, and",
        "`pylance` track the client's installed versions via",
        "`geneva_examples/core/package_specs.py` (`package_spec`); the rest are",
        "pinned in the module named in each row. Every spec can be overridden",
        "verbatim by its environment variable.",
        "",
        "## Manifests",
        "",
        "| Manifest | Module | Pip specs |",
        "|---|---|---|",
    ]
    for pip_attr, module_path, specs in manifests:
        rendered = (
            ", ".join(f"`{escape_cell(spec)}`" for spec in specs)
            if specs
            else "*(empty — geneva builtins only)*"
        )
        lines.append(f"| `{pip_attr}` | `{module_path}` | {rendered} |")

    lines += [
        "",
        "## Override environment variables",
        "",
        "Setting one of these before a stage command replaces the spec verbatim",
        "in every manifest built afterwards (it need not be an `==` pin).",
        "",
        "| Env var | Current value | Defined in |",
        "|---|---|---|",
    ]
    by_var: dict[str, dict[str, list[str]]] = {}
    order: list[str] = []
    for var in spec_vars:
        if var.env_var not in by_var:
            by_var[var.env_var] = {}
            order.append(var.env_var)
        by_var[var.env_var].setdefault(var.value, []).append(var.module_path)
    for env_var in order:
        for value, modules in by_var[env_var].items():
            modules_cell = ", ".join(f"`{m}`" for m in dict.fromkeys(modules))
            lines.append(f"| `{env_var}` | `{escape_cell(value)}` | {modules_cell} |")
    multi = [env for env in order if len(by_var[env]) > 1]
    if multi:
        names = ", ".join(f"`{env}`" for env in multi)
        verb = "resolves" if len(multi) == 1 else "resolve"
        lines += [
            "",
            f"Note: {names} {verb} to *different* defaults per module (one row",
            "per value above) — an override replaces every occurrence.",
        ]

    lines += [
        "",
        "## Deviations from the naming convention",
        "",
        "The convention (`geneva_examples/core/package_specs.py`,",
        "`_default_env_var`) is `{DISTRIBUTION}_PACKAGE_SPEC` with",
        "non-alphanumerics collapsed to `_`. Deviations:",
        "",
        "| Package | Conventional name | Actual env var read | Where |",
        "|---|---|---|---|",
    ]
    lines.extend(
        f"| `{package}` | `{conventional}` | `{actual}` | `{where}` |"
        for package, conventional, actual, where in DEVIATIONS
    )
    lines.append("")
    return "\n".join(lines)
