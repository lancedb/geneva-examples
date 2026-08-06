"""Unit tests for the docs generator's pure renderers and checkers."""

from __future__ import annotations

import click
import pytest

from geneva_examples.core.spec import Example, Param, Step
from geneva_examples.docs_gen.__main__ import main
from geneva_examples.docs_gen.linkcheck import (
    check_file,
    check_tree,
    github_slug,
    headings,
)
from geneva_examples.docs_gen.pins import assert_clean_spec_env
from geneva_examples.docs_gen.render import (
    CLI_DIR,
    default_label,
    escape_cell,
    escape_prose,
    flag_label,
    render_example_page,
    type_label,
)

# --- escaping ----------------------------------------------------------------


def test_escape_cell_flattens_and_escapes():
    assert escape_cell("a | b") == "a \\| b"
    assert escape_cell("line\nbreak") == "line break"
    assert escape_cell("list<float32> rows") == "list\\<float32\\> rows"


def test_escape_angles_leaves_code_spans_alone():
    text = "a `list<float32>` span and a bare list<float32>"
    escaped = escape_prose(text, context="test")
    assert "`list<float32>`" in escaped
    assert "bare list\\<float32\\>" in escaped


def test_escape_prose_refuses_heading_injection():
    with pytest.raises(ValueError, match="step 'evil'"):
        escape_prose("fine\n# sneaky heading", context="step 'evil'")


# --- click option labels ------------------------------------------------------


def _option(*decls: str, **kwargs) -> click.Option:
    return click.Option(list(decls), **kwargs)


def test_type_label_variants():
    assert type_label(_option("--n", type=click.IntRange(1, 100), default=1)) == (
        "int 1–100"
    )
    assert type_label(_option("--n", type=click.IntRange(0), default=0)) == ("int ≥ 0")
    assert type_label(_option("--x", type=click.FloatRange(max=2.0), default=1.0)) == (
        "float ≤ 2.0"
    )
    assert (
        type_label(_option("--m", type=click.Choice(["a", "b"]), default="a"))
        == "choice: a \\| b"
    )
    assert type_label(_option("--p", type=click.Path(), default=None)) == "path"
    assert type_label(_option("--s", type=str, default="x")) == "str"
    assert type_label(_option("--flag/--no-flag", default=True)) == "flag"


def test_default_and_flag_labels_for_bool_pairs():
    on = _option("--flag/--no-flag", default=True)
    off = _option("--flag/--no-flag", default=False)
    unset = _option("--flag/--no-flag", default=None)
    assert default_label(on) == "`--flag`"
    assert default_label(off) == "`--no-flag`"
    assert default_label(unset) == "`None`"
    assert flag_label(on) == "`--flag` / `--no-flag`"
    assert flag_label(_option("--name", default="x")) == "`--name`"


def test_default_label_escapes_pipes():
    assert default_label(_option("--s", type=str, default="a|b")) == "`a\\|b`"


# --- page rendering with a synthetic example ----------------------------------


def _synthetic_example() -> Example:
    def run(cfg, **kwargs) -> None: ...

    step = Step(
        key="demo-step",
        title="Demo step",
        description="Emits `list<float32>` rows and a bare list<float32>.",
        run=run,
        gpu=True,
        requires="run seed first",
        default_mode="local",
        params=(
            Param(name="count", type=int, default=3, help="How many.", min=1),
            Param(name="loud", type=bool, default=False, help="Log more."),
            Param(
                name="kind",
                type=str,
                default="a",
                help="Which kind.",
                choices=("a", "b"),
            ),
        ),
    )
    return Example(
        name="demo",
        title="Demo pipeline",
        description="A synthetic example for renderer tests.",
        modality="demo",
        steps=(step,),
    )


def test_render_example_page_shape():
    page = render_example_page(_synthetic_example())
    assert page.startswith("<!-- GENERATED FILE")
    assert "## `demo-step`" in page
    assert "pinned to `--mode local`" in page
    assert "yes — runs a model" in page
    assert "run seed first" in page
    assert "| `--count` | int ≥ 1 | `3` | How many. |" in page
    assert "| `--loud` / `--no-loud` | flag | `--no-loud` | Log more. |" in page
    assert "| `--kind` | choice: a \\| b | `a` | Which kind. |" in page
    # angle brackets escaped in prose, intact in code spans
    assert "`list<float32>`" in page
    assert "bare list\\<float32\\>" in page
    # the pinned local mode shows up as the common-option default too
    assert "| `--mode` | choice: local \\| enterprise | `local` |" in page


# --- github_slug / linkcheck ---------------------------------------------------


def test_github_slug():
    assert github_slug("Common options (every command)") == (
        "common-options-every-command"
    )
    assert github_slug("`ingest-images`") == "ingest-images"
    assert github_slug("Reset **vs** incremental") == "reset-vs-incremental"


def test_linkcheck_finds_broken_links_and_anchors(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "target.md").write_text("# Target\n\n## Real heading\n")
    good = "[ok](target.md) [ok2](target.md#real-heading) [self](#local)\n## Local\n"
    bad = "[gone](missing.md) [bad-anchor](target.md#nope)\n"
    fenced = "```\n[ignored](nowhere.md)\n```\n"
    (docs / "page.md").write_text(good + bad + fenced)
    problems = check_file(docs / "page.md", tmp_path)
    assert len(problems) == 2
    assert "broken link 'missing.md'" in problems[0]
    assert "missing anchor '#nope'" in problems[1]


def test_headings_skips_fenced_blocks(tmp_path):
    page = tmp_path / "page.md"
    page.write_text("# Top\n```\n# not a heading\n```\n## Sub\n")
    assert headings(page) == {"top", "sub"}


def test_check_tree_walks_docs(tmp_path):
    (tmp_path / "README.md").write_text("[broken](nope.md)\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("[ok](../README.md)\n")
    problems = check_tree(tmp_path)
    assert len(problems) == 1
    assert "README.md:1" in problems[0]


# --- env scrub -----------------------------------------------------------------


def test_assert_clean_spec_env_refuses_overrides(monkeypatch):
    monkeypatch.setenv("PYARROW_PACKAGE_SPEC", "pyarrow==1.0")
    with pytest.raises(RuntimeError, match="PYARROW_PACKAGE_SPEC"):
        assert_clean_spec_env()
    monkeypatch.delenv("PYARROW_PACKAGE_SPEC")
    monkeypatch.setenv("MMS_TTS_MODEL_ID", "facebook/other")
    with pytest.raises(RuntimeError, match="MMS_TTS_MODEL_ID"):
        assert_clean_spec_env()


# --- __main__ write / --check --------------------------------------------------


def test_main_write_then_check_roundtrip(tmp_path, capsys):
    assert main([], repo_root=tmp_path) == 0
    assert main(["--check"], repo_root=tmp_path) == 0

    # stale: mutate one generated file
    target = tmp_path / CLI_DIR / "images.md"
    target.write_text(target.read_text() + "tampered\n")
    assert main(["--check"], repo_root=tmp_path) == 1
    out = capsys.readouterr().out
    assert "stale: docs/reference/cli/images.md" in out
    assert "run `make docs`" in out

    # missing: remove it
    target.unlink()
    assert main(["--check"], repo_root=tmp_path) == 1
    assert "missing: docs/reference/cli/images.md" in capsys.readouterr().out

    # orphaned: an unexpected file in the machine-owned directory
    assert main([], repo_root=tmp_path) == 0
    (tmp_path / CLI_DIR / "rogue.md").write_text("hi\n")
    assert main(["--check"], repo_root=tmp_path) == 1
    assert "orphaned: docs/reference/cli/rogue.md" in capsys.readouterr().out
