"""Relative-link and anchor checker for the markdown docs tree.

Deliberately stdlib-only (~70 lines of regex + pathlib): for *relative* links
an external tool (lychee, markdown-link-check, linkchecker) is supply chain
for a trivial job. External ``https://`` links are out of scope — checking
them needs the network, which makes CI flaky.

GitHub's anchor slugs are approximated by :func:`github_slug`: lowercase,
markdown formatting stripped, characters outside ``[a-z0-9 _-]`` dropped,
spaces to hyphens. Duplicate-heading ``-N`` suffixes are not modeled — the
docs style is one topic per heading, so duplicates are a smell anyway.
"""

from __future__ import annotations

import re
from pathlib import Path

LINK_RE = re.compile(r"\[[^\]]*\]\((?!https?://|mailto:)([^)#\s]*)(#[^)\s]*)?\)")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$")
_FENCE_RE = re.compile(r"^(```|~~~)")
_FORMATTING_RE = re.compile(r"[`*]")


def github_slug(heading: str) -> str:
    """GitHub's anchor slug for a heading (sufficient approximation)."""
    text = _FORMATTING_RE.sub("", heading.strip()).lower()
    text = re.sub(r"[^a-z0-9 _-]", "", text)
    return text.replace(" ", "-")


def _visible_lines(path: Path) -> list[str]:
    """The file's lines with fenced code blocks blanked out."""
    lines: list[str] = []
    in_fence = False
    for line in path.read_text().splitlines():
        if _FENCE_RE.match(line.strip()):
            in_fence = not in_fence
            lines.append("")
            continue
        lines.append("" if in_fence else line)
    return lines


def headings(path: Path) -> set[str]:
    """Anchor slugs of every heading in a markdown file."""
    slugs = set()
    for line in _visible_lines(path):
        match = _HEADING_RE.match(line)
        if match:
            slugs.add(github_slug(match.group(1)))
    return slugs


def check_file(path: Path, repo_root: Path) -> list[str]:
    """Problems with the relative links in one markdown file."""
    problems: list[str] = []
    rel = path.relative_to(repo_root)
    for number, line in enumerate(_visible_lines(path), start=1):
        for match in LINK_RE.finditer(line):
            target, anchor = match.group(1), match.group(2)
            if target:
                resolved = (path.parent / target).resolve()
                if not resolved.exists():
                    problems.append(f"{rel}:{number}: broken link {target!r}")
                    continue
            else:
                resolved = path  # pure-anchor link into the same file
            if (
                anchor
                and resolved.suffix == ".md"
                and anchor[1:] not in headings(resolved)
            ):
                problems.append(
                    f"{rel}:{number}: missing anchor {anchor!r} in "
                    f"{resolved.relative_to(repo_root)}"
                )
    return problems


def check_tree(repo_root: Path) -> list[str]:
    """Check every documentation root plus the whole docs/ tree."""
    roots = [
        repo_root / "README.md",
        repo_root / "AUTHORING.md",
        repo_root / "CLI_ARCHITECTURE.md",
        repo_root / "CONTRIBUTING.md",
        repo_root / "SECURITY.md",
        repo_root / "studio_data" / "README.md",
        repo_root / "reports" / "README.md",
        *sorted((repo_root / "docs").rglob("*.md")),
    ]
    problems: list[str] = []
    for path in roots:
        if path.exists():
            problems.extend(check_file(path, repo_root))
    return problems
