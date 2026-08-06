"""Render the repo-root ``llms.txt`` from a curated link manifest.

``llms.txt`` follows the llmstxt.org shape: an H1 (the only required element),
a blockquote summary, one prose paragraph, then H2-delimited link lists of
``- [name](path): note`` items. The reserved ``## Optional`` section holds
links an agent may skip under context pressure.

The curation lives here as data — editing the link list is a reviewed code
change, and validation (paths exist, 10–20 links, item shape) is plain unit
tests — rather than as a hand-maintained file that drifts. Paths are
repo-relative because the primary consumers are coding agents holding a
checkout; remote readers resolve them against the GitHub raw URL root.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LlmsLink:
    """One curated llms.txt entry."""

    section: str  # "Docs" | "Reference" | "Optional"
    name: str
    path: str  # repo-relative, forward slashes
    note: str


LINKS: tuple[LlmsLink, ...] = (
    LlmsLink(
        "Docs",
        "Documentation index",
        "docs/README.md",
        "every page with one-line descriptions, term conventions, and grep hints",
    ),
    LlmsLink(
        "Docs",
        "Configuration and modes",
        "docs/getting-started/configuration.md",
        "all config.yaml keys, mode precedence, credential sets, R2 region rule",
    ),
    LlmsLink(
        "Docs",
        "Local-mode behavior",
        "docs/reference/local-mode.md",
        "exactly how every resource knob is clamped on a laptop",
    ),
    LlmsLink(
        "Docs",
        "Backfills: reset vs incremental",
        "docs/concepts/backfills.md",
        "the destructive-default contract and the overlap invariant",
    ),
    LlmsLink(
        "Docs",
        "Materialized views and stable row IDs",
        "docs/concepts/materialized-views.md",
        "the unrecoverable-view invariant every table creation guards against",
    ),
    LlmsLink(
        "Docs",
        "Spec and CLI generation",
        "docs/concepts/spec-and-cli-generation.md",
        "how Example → Step → Param becomes commands and TUI forms",
    ),
    LlmsLink(
        "Docs",
        "Environment variables",
        "docs/reference/environment-variables.md",
        "*_PACKAGE_SPEC overrides, the ASSETS_S3_* worker contract, and the "
        "variables the code sets itself",
    ),
    LlmsLink(
        "Docs",
        "Tables and schemas",
        "docs/reference/tables-and-schemas.md",
        "every column each pipeline produces, plus geneva_jobs/geneva_errors",
    ),
    LlmsLink(
        "Docs",
        "Adding a step",
        "docs/authoring/adding-a-step.md",
        "the single checklist for adding steps and whole examples",
    ),
    LlmsLink(
        "Docs",
        "Writing UDFs",
        "docs/authoring/writing-udfs.md",
        "closure rules, manifests, worker credentials",
    ),
    LlmsLink(
        "Docs",
        "Testing guide",
        "docs/authoring/testing.md",
        "the two-tier geneva mock, the smoke-test recipe, coverage-gate mechanics",
    ),
    LlmsLink(
        "Docs",
        "Glossary",
        "docs/reference/glossary.md",
        "one definition per term, each anchored to a code path",
    ),
    LlmsLink(
        "Reference",
        "Command index",
        "docs/reference/cli/index.md",
        "every console script, generated from the spec registry",
    ),
    LlmsLink(
        "Reference",
        "CLI reference: video",
        "docs/reference/cli/video.md",
        "generated flag tables for the 10 video steps",
    ),
    LlmsLink(
        "Reference",
        "CLI reference: images",
        "docs/reference/cli/images.md",
        "generated flag tables for the 4 image steps",
    ),
    LlmsLink(
        "Reference",
        "Worker runtime pins",
        "docs/reference/worker-runtime-pins.md",
        "per-manifest pip specs and *_PACKAGE_SPEC env overrides",
    ),
    LlmsLink(
        "Optional",
        "Troubleshooting",
        "docs/operations/troubleshooting.md",
        "symptom → cause → fix, including client/driver pin skew",
    ),
    LlmsLink(
        "Optional",
        "Version pins and cluster upgrades",
        "docs/operations/version-pins.md",
        "the two pin tiers and the upgrade runbook",
    ),
)

_HEADER = """\
# geneva-examples

> Runnable example Geneva UDF pipelines for LanceDB: five examples (images,
> video, pdf, audio, debugging) that ingest media into LanceDB tables and
> backfill feature columns or build chunker materialized views. One
> declarative spec per step generates both the CLI commands
> (`uv run <command>`) and the interactive TUI. Runs in local mode (on-disk
> Lance database, zero config) or enterprise mode (LanceDB Enterprise + a
> remote Geneva runtime).

Paths are repo-relative; remote readers resolve them against the repository's
raw URL root. The registry of examples is
`geneva_examples/examples/__init__.py`, and the CLI reference below is
generated from it. Setup is `make install`; the CI gate is `make check`.
The complete docs corpus concatenated into one file is `llms-full.txt`.
"""


def render_llms_txt() -> str:
    """The llms.txt content, sections in Docs / Reference / Optional order."""
    parts = [_HEADER]
    for section in ("Docs", "Reference", "Optional"):
        parts.append(f"\n## {section}\n")
        parts.extend(
            f"- [{link.name}]({link.path}): {link.note}"
            for link in LINKS
            if link.section == section
        )
    return "\n".join(parts) + "\n"


_FULL_HEADER = """\
# geneva-examples — full documentation corpus

Every page under docs/ concatenated for single-fetch ingestion, in index-first
order. Each page is delimited by a `<!-- source: <path> -->` comment plus a
visible `Source: <path>` line (relative links inside a page resolve against
its source path). Regenerated by `make docs`; the curated index is `llms.txt`.
"""


def _full_order(repo_root: Path, rendered: dict[Path, str]) -> list[Path]:
    """docs/README.md first, then every docs page in repo-relative path order.

    Enumerates the union of on-disk pages and about-to-be-written generated
    pages, so a fresh tree converges on the first ``make docs`` run instead of
    needing a second pass to pick up the just-written CLI reference.
    """
    pages = {
        path.relative_to(repo_root)
        for path in (repo_root / "docs").rglob("*.md")
        if path.is_file()
    }
    pages.update(rel for rel in rendered if rel.parts[0] == "docs")
    index = Path("docs/README.md")
    rest = sorted(page for page in pages if page != index)
    return [index, *rest] if index in pages else rest


def render_llms_full(repo_root: Path, rendered: dict[Path, str]) -> str:
    """Concatenate the docs corpus into llms-full.txt.

    Generated pages come from ``rendered`` (the in-memory content about to be
    written) rather than from disk, so llms-full.txt can never lag the CLI
    reference within a single ``make docs`` run.
    """
    parts = [_FULL_HEADER]
    for rel in _full_order(repo_root, rendered):
        content = rendered.get(rel)
        if content is None:
            content = (repo_root / rel).read_text()
        parts.append(
            f"\n<!-- source: {rel.as_posix()} -->\n"
            f"---\n\n**Source: `{rel.as_posix()}`**\n\n{content.rstrip()}\n"
        )
    return "".join(parts)
