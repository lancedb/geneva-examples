"""Registry of self-contained example pipelines.

Each example lives in its own subpackage exporting an ``EXAMPLE`` spec. Both the
generated ``uv run <name>`` CLIs and the Textual TUI read this registry, so adding
an example here makes it available in both without further wiring.

Importing this module must stay cheap — the example modules declare their specs
without importing torch/geneva (those load lazily inside ``run``/factory bodies).
"""

from __future__ import annotations

from geneva_examples.core.spec import Example, Step
from geneva_examples.examples.audio import EXAMPLE as AUDIO
from geneva_examples.examples.debugging import EXAMPLE as DEBUGGING
from geneva_examples.examples.images import EXAMPLE as IMAGES
from geneva_examples.examples.pdf import EXAMPLE as PDF
from geneva_examples.examples.video import EXAMPLE as VIDEO

EXAMPLES: tuple[Example, ...] = (IMAGES, VIDEO, PDF, AUDIO, DEBUGGING)


def all_examples() -> tuple[Example, ...]:
    return EXAMPLES


def get_example(name: str) -> Example:
    for ex in EXAMPLES:
        if ex.name == name:
            return ex
    raise KeyError(f"no example named {name!r}")


def iter_steps() -> list[tuple[Example, Step]]:
    """All (example, step) pairs, in registry order."""
    return [(ex, step) for ex in EXAMPLES for step in ex.steps]
