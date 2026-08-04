"""Repo-level docs invariants: freshness, completeness, links, conventions.

These tests are the anti-drift half of the docs design: the generated pages
must match the spec registry byte-for-byte, every registered step must appear
in the reference, the hand-written schema page must name every spec-declared
table/column default, and every relative link in the tree must resolve.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from geneva_examples.docs_gen.linkcheck import check_tree
from geneva_examples.docs_gen.llms import LINKS, render_llms_txt
from geneva_examples.docs_gen.pins import DEVIATIONS, collect
from geneva_examples.docs_gen.render import CLI_DIR, GENERATED_MARKER, render_all
from geneva_examples.examples import EXAMPLES, iter_steps
from geneva_examples.ops.stats import _DEFAULT_TABLES

REPO = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (REPO / rel).read_text()


# --- generated docs: freshness and completeness --------------------------------


def test_generated_docs_are_fresh():
    """Committed generated files match a live re-render (`make docs` fixes)."""
    for rel, content in render_all(REPO).items():
        on_disk = REPO / rel
        assert on_disk.exists(), f"missing generated file: {rel} — run `make docs`"
        assert on_disk.read_text() == content, (
            f"stale generated file: {rel} — run `make docs` and commit"
        )


def test_every_registered_step_has_a_reference_section():
    index = _read("docs/reference/cli/index.md")
    for example, step in iter_steps():
        page = _read(f"docs/reference/cli/{example.name}.md")
        assert f"## `{step.key}`" in page, (
            f"{step.key} missing from docs/reference/cli/{example.name}.md"
        )
        assert f"[`{step.key}`]({example.name}.md#{step.key})" in index


def test_cli_reference_page_set_matches_registry():
    expected = {f"{example.name}.md" for example in EXAMPLES} | {"index.md"}
    actual = {path.name for path in (REPO / CLI_DIR).iterdir()}
    assert actual == expected


def test_every_console_script_is_in_the_command_index():
    with (REPO / "pyproject.toml").open("rb") as fh:
        scripts = tomllib.load(fh)["project"]["scripts"]
    index = _read("docs/reference/cli/index.md")
    for name in scripts:
        assert f"`{name}`" in index, f"console script {name} missing from index"


def _option_row(page: str, command: str, flag: str) -> str:
    """The options-table row for a flag within one command's section."""
    section = page.split(f"## `{command}`")[1].split("\n## ")[0]
    rows = [line for line in section.splitlines() if line.startswith(f"| `{flag}`")]
    assert rows, f"no row for {flag} under {command}"
    return rows[0]


def test_reference_defaults_match_specs():
    """Spot-check that rendered defaults come from the registry, not prose."""
    by_key = {step.key: (example, step) for example, step in iter_steps()}

    video = _read("docs/reference/cli/video.md")
    _, chunk = by_key["chunk-videos"]
    chunk_seconds = next(p for p in chunk.params if p.name == "chunk_seconds")
    assert f"`{chunk_seconds.default}`" in _option_row(
        video, "chunk-videos", "--chunk-seconds"
    )
    assert "`--no-reset`" in _option_row(video, "frame-embed", "--reset")

    images = _read("docs/reference/cli/images.md")
    _, ingest = by_key["ingest-images"]
    num_images = next(p for p in ingest.params if p.name == "num_images")
    assert f"`{num_images.default}`" in _option_row(
        images, "ingest-images", "--num-images"
    )


# --- llms.txt -------------------------------------------------------------------


def test_llms_txt_links_resolve():
    for link in LINKS:
        assert (REPO / link.path).exists(), f"llms.txt links missing file {link.path}"


def test_llms_txt_shape():
    text = render_llms_txt()
    lines = text.splitlines()
    assert lines[0] == "# geneva-examples"
    first_content = next(line for line in lines[1:] if line.strip())
    assert first_content.startswith("> ")
    items = [line for line in lines if line.startswith("- [")]
    assert 10 <= len(items) <= 20
    item_re = re.compile(r"^- \[[^\]]+\]\([^)]+\): .+$")
    for item in items:
        assert item_re.match(item), f"malformed llms.txt item: {item}"


def test_llms_full_covers_every_docs_page():
    full = _read("llms-full.txt")
    for path in sorted((REPO / "docs").rglob("*.md")):
        rel = path.relative_to(REPO).as_posix()
        assert f"<!-- source: {rel} -->" in full, f"{rel} missing from llms-full.txt"


# --- links, schema drift, ops claims ---------------------------------------------


def test_relative_links_and_anchors_resolve():
    assert check_tree(REPO) == []


def test_tables_reference_mentions_all_spec_tables_and_columns():
    """The hand-written schema page must name every spec-declared default."""
    page = _read("docs/reference/tables-and-schemas.md")
    tracked = ("table_name", "source_table", "clips_table", "output_column")
    for _, step in iter_steps():
        for param in step.params:
            if param.name in tracked and isinstance(param.default, str):
                assert f"`{param.default}`" in page, (
                    f"{param.default!r} (step {step.key}, param {param.name}) "
                    "missing from docs/reference/tables-and-schemas.md"
                )


def test_index_ops_claims_match_code():
    index = _read("docs/reference/cli/index.md")
    stats_row = next(
        line for line in index.splitlines() if line.startswith("| `stats` |")
    )
    for table in _DEFAULT_TABLES:
        assert f"`{table}`" in stats_row


def test_package_spec_env_vars_all_documented():
    """Every *_PACKAGE_SPEC env literal in source is on the pins page."""
    literal_re = re.compile(r'"([A-Z0-9_]+_PACKAGE_SPEC)"')
    literals: set[str] = set()
    for path in (REPO / "geneva_examples").rglob("*.py"):
        if "docs_gen" in path.parts or path.name == "package_specs.py":
            continue
        literals.update(literal_re.findall(path.read_text()))

    _, spec_vars = collect()
    documented_env_reads = {v.env_var for v in spec_vars if not v.tracks_installed}
    assert literals == documented_env_reads

    # The convention deviation stays honest: if clip.py is ever fixed to read
    # the rule-derived name, this forces the DEVIATIONS table entry's removal.
    for _package, conventional, actual, _where in DEVIATIONS:
        assert actual in literals
        assert conventional not in literals


# --- LLM-convention conformance ---------------------------------------------------


def test_docs_llm_conventions():
    index = REPO / "docs" / "README.md"
    assert index.exists()
    assert len(index.read_text().splitlines()) < 500

    generated = {REPO / rel for rel in render_all(REPO)}
    for path in sorted((REPO / "docs").rglob("*.md")):
        lines = path.read_text().splitlines()
        if len(lines) > 100 and path != index:
            head = "\n".join(lines[:40])
            assert "## Contents" in head, f"{path} lacks a Contents list up top"
        if path in generated:
            assert lines[0] == GENERATED_MARKER, f"{path} lacks the HTML marker"
            assert "Generated file — do not edit" in "\n".join(lines[:12]), (
                f"{path} lacks the visible generated-file notice"
            )
