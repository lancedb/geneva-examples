"""Tests for YAML config loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from geneva_examples.core.config import DEFAULT_DB_URI, DEFAULT_TABLE_NAME, load_config


def _write(path: Path, body: str) -> Path:
    path.write_text(body)
    return path


def test_load_minimal_applies_defaults(tmp_path: Path):
    cfg = load_config(
        _write(
            tmp_path / "c.yaml",
            "lancedb_api_key: key\nlancedb_region: us-east-1\ngeneva_host: host:80\n",
        )
    )
    assert cfg.lancedb_api_key == "key"
    assert cfg.db_uri == DEFAULT_DB_URI
    assert cfg.table_name == DEFAULT_TABLE_NAME
    assert cfg.storage_options() is None


def test_storage_options_requires_all_four_r2_fields(tmp_path: Path):
    body = (
        "lancedb_api_key: k\nlancedb_region: r\ngeneva_host: h\n"
        "r2_access_key: a\nr2_secret_key: s\nr2_endpoint: e\nr2_region: auto\n"
    )
    opts = load_config(_write(tmp_path / "c.yaml", body)).storage_options()
    assert opts["aws_access_key_id"] == "a"
    assert opts["aws_region"] == "auto"


def test_storage_options_none_when_partial(tmp_path: Path):
    body = "lancedb_api_key: k\nlancedb_region: r\ngeneva_host: h\nr2_access_key: a\n"
    assert load_config(_write(tmp_path / "c.yaml", body)).storage_options() is None


def test_missing_required_field_raises(tmp_path: Path):
    with pytest.raises(RuntimeError, match="missing required config"):
        load_config(_write(tmp_path / "c.yaml", "lancedb_api_key: k\n"))


def test_missing_file_raises(tmp_path: Path):
    with pytest.raises(RuntimeError, match="config file not found"):
        load_config(tmp_path / "absent.yaml")


def test_load_config_defaults_to_cwd_config_yaml(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path / "config.yaml",
        "lancedb_api_key: k\nlancedb_region: r\ngeneva_host: h\n",
    )
    cfg = load_config()  # no path -> ./config.yaml
    assert cfg.lancedb_api_key == "k"
