"""Tests for the example registry."""

from __future__ import annotations

import subprocess
import sys

import pytest

import geneva_examples.examples as reg


def test_examples_present_in_order():
    assert [e.name for e in reg.all_examples()] == ["images", "video", "pdf", "audio"]


def test_step_keys_unique_and_described():
    seen: set[str] = set()
    for _ex, step in reg.iter_steps():
        assert step.key not in seen, f"duplicate step key {step.key}"
        seen.add(step.key)
        assert step.description.strip()
        assert step.title.strip()
        assert callable(step.run)


def test_get_example():
    assert reg.get_example("images").modality == "image"
    with pytest.raises(KeyError):
        reg.get_example("nope")


def test_registry_import_is_cheap():
    # Importing the registry to list/describe examples must NOT drag in the ML
    # stack — run in a fresh interpreter so other tests' imports don't pollute.
    code = (
        "import sys; import geneva_examples.examples as r; "
        "assert [e.name for e in r.all_examples()]; "
        "assert 'torch' not in sys.modules, 'torch imported'; "
        "assert 'geneva' not in sys.modules, 'geneva imported'; "
        "print('ok')"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
