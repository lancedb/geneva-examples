"""Entry point: write the generated docs, or verify them with ``--check``.

``uv run python -m geneva_examples.docs_gen`` rewrites every generated file
(``make docs``). ``--check`` renders everything in memory and diffs against
disk without writing — it does not use ``git diff``, so unrelated dirty files
cannot cause false failures — and reports three states: *stale* (content
differs), *missing* (expected file absent), and *orphaned* (an unexpected file
inside the machine-owned ``docs/reference/cli/`` directory).
"""

from __future__ import annotations

import sys
from pathlib import Path

from geneva_examples.docs_gen.pins import assert_clean_spec_env
from geneva_examples.docs_gen.render import CLI_DIR, render_all


def _check(root: Path, rendered: dict[Path, str]) -> list[str]:
    problems: list[str] = []
    for rel, content in rendered.items():
        path = root / rel
        if not path.exists():
            problems.append(f"missing: {rel}")
        elif path.read_text() != content:
            problems.append(f"stale: {rel}")
    expected = {root / rel for rel in rendered}
    cli_dir = root / CLI_DIR
    if cli_dir.is_dir():
        problems.extend(
            f"orphaned: {path.relative_to(root)}"
            for path in sorted(cli_dir.glob("*"))
            if path not in expected
        )
    return problems


def main(argv: list[str] | None = None, repo_root: Path | None = None) -> int:
    """Write the generated docs (default) or verify them (``--check``)."""
    args = list(sys.argv[1:]) if argv is None else list(argv)
    root = repo_root or Path(__file__).resolve().parents[2]
    assert_clean_spec_env()
    rendered = render_all(root)
    if "--check" in args:
        problems = _check(root, rendered)
        if problems:
            for problem in problems:
                print(problem)
            print("generated docs are out of date: run `make docs` and commit")
            return 1
        print(f"generated docs are fresh ({len(rendered)} files)")
        return 0
    for rel, content in rendered.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        print(f"wrote {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
