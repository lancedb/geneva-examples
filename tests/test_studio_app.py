"""Tests for UDF Studio app helpers and UI construction."""

from __future__ import annotations

from pathlib import Path

import pytest

from geneva_examples.apps import udf_studio
from geneva_examples.apps.studio import library


def test_describe_bytes_and_text():
    assert udf_studio._describe(b"abcd") == "4 bytes"
    assert udf_studio._describe("short") == '"short"'
    long = udf_studio._describe("x" * 100)
    assert long.startswith('"') and long.endswith('…"')


def test_rows_to_df():
    assert udf_studio._rows_to_df([]).empty
    df = udf_studio._rows_to_df([{"row": 0, "output": "5"}])
    assert list(df.columns) == ["row", "output"]
    assert len(df) == 1


def test_saved_names(library_path: Path):
    assert udf_studio._saved_names(library_path) == []
    library.save_udf(library_path, "a", "udf", "image", "code")
    assert udf_studio._saved_names(library_path) == ["a"]


def test_saved_names_handles_bad_path(tmp_path: Path):
    # A path that can't be a library should be swallowed (returns []), not raise.
    bad = tmp_path / "afile"
    bad.write_text("not a db")
    assert udf_studio._saved_names(bad) == []


def test_build_ui_constructs(tmp_path: Path):
    gr = pytest.importorskip("gradio")
    demo = udf_studio.build_ui(
        data_dir=str(tmp_path), library_path=str(tmp_path / "lib")
    )
    assert isinstance(demo, gr.Blocks)
