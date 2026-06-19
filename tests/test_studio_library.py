"""Tests for the local LanceDB UDF library."""

from __future__ import annotations

from pathlib import Path

import pytest

from geneva_examples.apps.studio import library


def test_save_list_load_roundtrip(library_path: Path):
    library.save_udf(
        library_path, "dims", "udf", "image", "def transform(v):\n    return len(v)\n"
    )
    items = library.list_udfs(library_path)
    assert [r["name"] for r in items] == ["dims"]
    loaded = library.load_udf(library_path, "dims")
    assert loaded["kind"] == "udf"
    assert loaded["modality"] == "image"
    assert "transform" in loaded["code"]


def test_save_overwrites_by_name(library_path: Path):
    library.save_udf(library_path, "f", "udf", "image", "v1")
    library.save_udf(library_path, "f", "udf", "text", "v2")
    items = [r for r in library.list_udfs(library_path) if r["name"] == "f"]
    assert len(items) == 1
    assert library.load_udf(library_path, "f")["code"] == "v2"
    assert library.load_udf(library_path, "f")["modality"] == "text"


def test_delete(library_path: Path):
    library.save_udf(library_path, "a", "udf", "image", "x")
    library.save_udf(library_path, "b", "chunker", "video", "y")
    library.delete_udf(library_path, "a")
    assert [r["name"] for r in library.list_udfs(library_path)] == ["b"]


def test_list_empty_before_any_save(library_path: Path):
    assert library.list_udfs(library_path) == []


def test_load_missing_raises(library_path: Path):
    library.save_udf(library_path, "a", "udf", "image", "x")
    with pytest.raises(KeyError):
        library.load_udf(library_path, "ghost")


def test_load_before_any_save_raises(library_path: Path):
    # No table exists yet at all.
    with pytest.raises(KeyError):
        library.load_udf(library_path, "anything")


def test_save_requires_name(library_path: Path):
    with pytest.raises(ValueError, match="name is required"):
        library.save_udf(library_path, "  ", "udf", "image", "x")


def test_name_with_single_quote(library_path: Path):
    library.save_udf(library_path, "o'brien", "udf", "text", "code")
    assert library.load_udf(library_path, "o'brien")["code"] == "code"
    library.delete_udf(library_path, "o'brien")
    assert library.list_udfs(library_path) == []
