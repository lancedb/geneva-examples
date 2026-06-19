"""Tests for UDF Studio directory-based sampling."""

from __future__ import annotations

from pathlib import Path

import pytest

from geneva_examples.apps.studio import samples


def test_sample_images_reads_bytes_and_filters_extensions(data_dir: Path):
    res = samples.sample(data_dir, "image", n=10)
    # notes.txt is excluded; the three PNGs come back as bytes, sorted by name.
    assert res["labels"] == ["img_0.png", "img_1.png", "img_2.png"]
    assert all(isinstance(v, bytes) for v in res["values"])
    assert res["modality"] == "image"
    assert "images/" in res["detail"]


def test_sample_respects_n(data_dir: Path):
    assert len(samples.sample(data_dir, "image", n=2)["values"]) == 2


def test_sample_text_named_column(data_dir: Path):
    res = samples.sample(data_dir, "text", n=5, csv_column="text")
    assert res["values"] == ["hello world", "the quick brown fox"]
    assert "column 'text'" in res["detail"]


def test_sample_text_defaults_to_first_column(data_dir: Path):
    res = samples.sample(data_dir, "text", n=1)
    assert res["values"] == ["1"]  # first column is 'id'


def test_csv_columns(data_dir: Path, tmp_path: Path):
    assert samples.csv_columns(data_dir) == ["id", "text"]
    assert samples.csv_columns(tmp_path / "nope") == []


def test_sample_missing_media_dir_raises(tmp_path: Path):
    (tmp_path / "images").mkdir()
    with pytest.raises(FileNotFoundError):
        samples.sample(tmp_path, "image", n=1)


def test_sample_modality_dir_absent_raises(tmp_path: Path):
    # videos/ doesn't exist at all -> _list_files returns [] -> FileNotFoundError.
    with pytest.raises(FileNotFoundError):
        samples.sample(tmp_path, "video", n=1)


def test_sample_unknown_modality(data_dir: Path):
    with pytest.raises(ValueError, match="unknown modality"):
        samples.sample(data_dir, "hologram", n=1)


def test_sample_bad_csv_column(data_dir: Path):
    with pytest.raises(ValueError, match="not in"):
        samples.sample(data_dir, "text", n=1, csv_column="missing")


def test_sample_missing_csv(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        samples.sample(tmp_path, "text", n=1)


def test_sample_text_empty_csv_has_no_columns(tmp_path: Path):
    (tmp_path / "input.csv").write_text("")
    with pytest.raises(ValueError, match="no columns"):
        samples.sample(tmp_path, "text", n=1)


def test_modalities_constant():
    assert samples.MODALITIES == ["image", "video", "audio", "text"]
