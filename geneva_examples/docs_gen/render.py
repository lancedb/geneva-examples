"""Pure renderers for the generated CLI reference pages.

Every page is rendered from the same objects the real CLIs are built from: for
each ``(example, step)`` pair the renderer calls
:func:`geneva_examples.core.spec.build_command` and walks the returned
``click.Command``'s params, so the option tables (flags, types, defaults, help)
match ``uv run <command> --help`` by construction. Step/Example metadata
(description, gpu, requires, default_mode, pipeline order) comes from the spec
objects themselves.

Determinism rules (the ``--check`` mode diffs bytes): iterate in registry
order, never sort by name; no timestamps; LF endings with a single trailing
newline; description text is emitted verbatim (escaped, never reflowed).
"""

from __future__ import annotations

import re
from pathlib import Path

import click

from geneva_examples.core.spec import Example, Step, build_command
from geneva_examples.examples import EXAMPLES

# Both markers are load-bearing: LLM parsers strip HTML comments, so the
# visible blockquote in each page header is the copy agents actually see.
GENERATED_MARKER = "<!-- GENERATED FILE — do not edit. `make docs` regenerates it. -->"

CLI_DIR = Path("docs/reference/cli")
PINS_PAGE = Path("docs/reference/worker-runtime-pins.md")
LLMS_TXT = Path("llms.txt")
LLMS_FULL_TXT = Path("llms-full.txt")

# The options build_command() adds to every command (by click param name).
_COMMON_PARAM_NAMES = ("config", "mode", "db_uri", "log_level")

# Console scripts that are not generated from the spec registry. Kept as a
# static table here (still single-sourced in code); tests cross-check it
# against [project.scripts] and ops.stats._DEFAULT_TABLES.
OPERATOR_TOOLS: tuple[tuple[str, str, str], ...] = (
    (
        "tui",
        "Interactive Textual runner: Tables / Jobs / Examples over the same specs",
        "geneva_examples/tui/app.py",
    ),
    (
        "stats",
        "Table row counts, schema, feature-column population (defaults: "
        "`images`, `videos`, `video_clips`; pass `--table` for `pdfs`/"
        "`audio`/`debug_demo`)",
        "geneva_examples/ops/stats.py",
    ),
    (
        "jobs",
        "List, show, tail, and kill Geneva backfill job records",
        "geneva_examples/ops/jobs.py",
    ),
    (
        "cleanup",
        "Drop `videos`/`video_clips`, optionally a pdfs table",
        "geneva_examples/ops/cleanup.py",
    ),
    (
        "udf-studio",
        "Gradio UDF prototyping sandbox (runs editor code unsandboxed — loopback only)",
        "geneva_examples/apps/udf_studio.py",
    ),
)

_CODE_SPAN_RE = re.compile(r"`[^`]+`")
_TAG_RE = re.compile(r"<(/?[A-Za-z][^<>]*)>")
_HEADING_LINE_RE = re.compile(r"^ {0,3}#")

_TYPE_NAMES = {"integer": "int", "float": "float", "text": "str", "boolean": "bool"}


def _escape_angles(text: str) -> str:
    """Backslash-escape tag-like ``<...>`` runs outside backtick code spans.

    Spec descriptions legitimately contain type expressions like
    ``list<float32>``; GitHub's HTML sanitizer would otherwise swallow the
    ``<float32>`` as an unknown tag.
    """
    parts: list[str] = []
    last = 0
    for match in _CODE_SPAN_RE.finditer(text):
        parts.append(_TAG_RE.sub(r"\\<\1\\>", text[last : match.start()]))
        parts.append(match.group(0))
        last = match.end()
    parts.append(_TAG_RE.sub(r"\\<\1\\>", text[last:]))
    return "".join(parts)


def escape_prose(text: str, *, context: str) -> str:
    """Escape a description for prose position; refuse heading injection.

    A spec description line that would parse as a markdown heading could
    silently restructure the generated page (and its anchors), so it is a
    hard error naming the offending step rather than something to escape.
    """
    for line in text.splitlines():
        if _HEADING_LINE_RE.match(line):
            raise ValueError(
                f"{context}: description line would render as a markdown "
                f"heading: {line!r}"
            )
    return _escape_angles(text)


def escape_cell(text: str) -> str:
    """Escape text for a markdown table cell (pipes break rows, even in code)."""
    flattened = " ".join(text.split())
    return _escape_angles(flattened).replace("|", "\\|")


def type_label(opt: click.Option) -> str:
    """Human-readable type for the options table."""
    if opt.secondary_opts:
        return "flag"
    param_type = opt.type
    if isinstance(param_type, click.Choice):
        return "choice: " + " \\| ".join(str(c) for c in param_type.choices)
    if isinstance(param_type, click.IntRange | click.FloatRange):
        base = "int" if isinstance(param_type, click.IntRange) else "float"
        if param_type.min is not None and param_type.max is not None:
            return f"{base} {param_type.min}\u2013{param_type.max}"
        if param_type.min is not None:
            return f"{base} \u2265 {param_type.min}"
        if param_type.max is not None:
            return f"{base} \u2264 {param_type.max}"
        return base
    if isinstance(param_type, click.Path):
        return "path"
    return _TYPE_NAMES.get(param_type.name, param_type.name)


def default_label(opt: click.Option) -> str:
    """Rendered default: bool pairs show the active flag, everything else `str`."""
    if opt.secondary_opts:
        if opt.default is None:
            return "`None`"
        return f"`{opt.opts[0]}`" if opt.default else f"`{opt.secondary_opts[0]}`"
    if opt.default is None:
        return "`None`"
    return f"`{escape_cell(str(opt.default))}`"


def flag_label(opt: click.Option) -> str:
    """The flag column: ``--name`` or the ``--x` / `--no-x`` pair."""
    if opt.secondary_opts:
        return f"`{opt.opts[0]}` / `{opt.secondary_opts[0]}`"
    return f"`{opt.opts[0]}`"


def _split_params(
    cmd: click.Command,
) -> tuple[list[click.Option], list[click.Option]]:
    """Partition a command's options into (common, step-specific), in order."""
    common: list[click.Option] = []
    specific: list[click.Option] = []
    for param in cmd.params:
        if not isinstance(param, click.Option):
            continue
        if param.name in _COMMON_PARAM_NAMES:
            common.append(param)
        else:
            specific.append(param)
    return common, specific


def _options_table(options: list[click.Option]) -> str:
    lines = ["| Option | Type | Default | Description |", "|---|---|---|---|"]
    lines.extend(
        f"| {flag_label(opt)} | {type_label(opt)} | {default_label(opt)} "
        f"| {escape_cell(opt.help or '')} |"
        for opt in options
    )
    return "\n".join(lines)


def _source_path(step: Step) -> str:
    return step.run.__module__.replace(".", "/") + ".py"


def _spec_path(example: Example) -> str:
    return f"geneva_examples/examples/{example.name}/__init__.py"


def render_step_section(example: Example, step: Step) -> str:
    """One ``## `<command>``` section: description, fixed facts, options."""
    cmd = build_command(example, step)
    _, specific = _split_params(cmd)
    gpu = "yes — runs a model (CPU-only in local mode)" if step.gpu else "no (CPU-only)"
    requires = escape_cell(step.requires) if step.requires else "none"
    mode = (
        f"pinned to `--mode {step.default_mode}`"
        if step.default_mode
        else "config-driven (`--mode` unset)"
    )
    description = escape_prose(step.description.strip(), context=f"step {step.key!r}")
    parts = [
        f"## `{step.key}`",
        "",
        f"**{escape_cell(step.title)}**",
        "",
        description,
        "",
        "| | |",
        "|---|---|",
        f"| Run | `uv run {step.key} [OPTIONS]` |",
        f"| GPU | {gpu} |",
        f"| Prerequisite | {requires} |",
        f"| Mode default | {mode} |",
        f"| Source | `{_source_path(step)}` (`run()`), spec in "
        f"`{_spec_path(example)}` |",
        "",
        "In addition to the [common options](#common-options-every-command):",
        "",
    ]
    if specific:
        parts.append(_options_table(specific))
    else:
        parts.append("*(no step-specific options)*")
    return "\n".join(parts)


def _common_options_section(example: Example) -> str:
    """The shared four-option table, rendered from this page's own commands.

    ``--mode``'s default varies when a step pins ``default_mode`` (only
    ``demo-errors`` today), so the table is built from the page's first
    command and the default cell is generalized when the page's steps
    disagree.
    """
    first_cmd = build_command(example, example.steps[0])
    common, _ = _split_params(first_cmd)
    mode_defaults = {step.default_mode for step in example.steps}
    lines = [
        "## Common options (every command)",
        "",
        "Every command below is generated by "
        "`geneva_examples/core/spec.py` (`build_command`) and accepts these "
        "options in addition to its own:",
        "",
        "| Option | Type | Default | Description |",
        "|---|---|---|---|",
    ]
    for opt in common:
        default = default_label(opt)
        if opt.name == "mode" and len(mode_defaults) > 1:
            default = "varies — see each command's *Mode default* row"
        lines.append(
            f"| {flag_label(opt)} | {type_label(opt)} | {default} "
            f"| {escape_cell(opt.help or '')} |"
        )
    lines += [
        "",
        "A `None` default is resolved at runtime: `--mode` and `--db-uri` "
        "follow the config-file precedence, and some model steps resolve "
        "`--num-gpus None` to a per-step GPU fraction inside their `run()` "
        "(see the step's source file).",
    ]
    return "\n".join(lines)


def _page_header(title: str, blockquote_lines: list[str]) -> str:
    quote = "\n".join(f"> {line}" for line in blockquote_lines)
    return f"{GENERATED_MARKER}\n# {title}\n\n{quote}\n"


def render_example_page(example: Example) -> str:
    """The full generated reference page for one example."""
    order = " \u2192 ".join(f"[`{step.key}`](#{step.key})" for step in example.steps)
    header = _page_header(
        f"`{example.name}` — {escape_cell(example.title)}: CLI reference",
        [
            "**Generated file — do not edit.** Rendered from the spec registry",
            f"(`{_spec_path(example)}`) by `geneva_examples/docs_gen/`;",
            "regenerate with `make docs`. The same text is shown by "
            "`uv run <command> --help`.",
            "Docs index: [`docs/README.md`](../../README.md).",
        ],
    )
    description = escape_prose(
        example.description.strip(), context=f"example {example.name!r}"
    )
    contents = ["## Contents", "", "- [Common options](#common-options-every-command)"]
    contents.extend(f"- [`{step.key}`](#{step.key})" for step in example.steps)
    sections = [render_step_section(example, step) for step in example.steps]
    return "\n".join(
        [
            header,
            description,
            "",
            f"**Modality:** `{example.modality}` · **Steps in pipeline order:** "
            f"{order}",
            "",
            "\n".join(contents),
            "",
            _common_options_section(example),
            "",
            "\n\n".join(sections),
            "",
        ]
    )


def render_command_index(examples: tuple[Example, ...]) -> str:
    """The command index: every pipeline command plus the operator tools."""
    total = sum(len(ex.steps) for ex in examples)
    header = _page_header(
        "Command index",
        [
            "**Generated file — do not edit.** Rendered from the spec registry by",
            "`geneva_examples/docs_gen/`; regenerate with `make docs`.",
            "Docs index: [`docs/README.md`](../../README.md).",
        ],
    )
    intro = (
        f"All {total} pipeline commands are generated from the spec registry\n"
        "(`geneva_examples/examples/__init__.py`); run any of them as\n"
        "`uv run <command>`. Grep hint: every command has an H2 heading equal\n"
        "to its name in the per-example pages, e.g.\n"
        '`grep -n "^## \\`chunk-videos\\`" docs/reference/cli/video.md`.'
    )
    rows = [
        "| Command | Example | What it does | GPU | Prerequisite |",
        "|---|---|---|---|---|",
    ]
    for example in examples:
        rows.extend(
            f"| [`{step.key}`]({example.name}.md#{step.key}) | {example.name} "
            f"| {escape_cell(step.title)} | {'yes' if step.gpu else 'no'} "
            f"| {escape_cell(step.requires) if step.requires else '—'} |"
            for step in example.steps
        )
    tools = [
        "## Operator tools (not spec-generated)",
        "",
        "| Command | What it does | Entrypoint |",
        "|---|---|---|",
    ]
    tools.extend(
        f"| `{name}` | {what} | `{entrypoint}` |"
        for name, what, entrypoint in OPERATOR_TOOLS
    )
    return "\n".join(
        [
            header,
            intro,
            "",
            "## Pipeline commands",
            "",
            "\n".join(rows),
            "",
            "\n".join(tools),
            "",
        ]
    )


def render_all(repo_root: Path) -> dict[Path, str]:
    """Every generated artifact, keyed by repo-relative path.

    The single source shared by ``__main__`` (write / ``--check``) and the
    test suite, so what tests assert is exactly what the CLI writes.
    ``repo_root`` is needed only by ``llms-full.txt``, which concatenates the
    hand-written docs pages from disk (generated pages come from this render,
    never from disk, so the corpus cannot lag the reference within one run).
    """
    from geneva_examples.docs_gen.llms import render_llms_full, render_llms_txt
    from geneva_examples.docs_gen.pins import render_pins_page

    out: dict[Path, str] = {}
    for example in EXAMPLES:
        out[CLI_DIR / f"{example.name}.md"] = render_example_page(example)
    out[CLI_DIR / "index.md"] = render_command_index(EXAMPLES)
    out[PINS_PAGE] = render_pins_page()
    out[LLMS_TXT] = render_llms_txt()
    out[LLMS_FULL_TXT] = render_llms_full(repo_root, out)
    return out
