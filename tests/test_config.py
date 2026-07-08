"""Tests for YAML config loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from geneva_examples.core.config import DEFAULT_DB_URI, load_config


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
    assert cfg.storage_options() is None


def test_storage_options_requires_all_four_s3_fields(tmp_path: Path):
    body = (
        "lancedb_api_key: k\nlancedb_region: r\ngeneva_host: h\n"
        "s3_access_key: a\ns3_secret_key: s\ns3_endpoint: e\ns3_region: auto\n"
    )
    opts = load_config(_write(tmp_path / "c.yaml", body)).storage_options()
    assert opts["aws_access_key_id"] == "a"
    assert opts["aws_region"] == "auto"


def test_storage_options_none_when_partial(tmp_path: Path):
    body = "lancedb_api_key: k\nlancedb_region: r\ngeneva_host: h\ns3_access_key: a\n"
    assert load_config(_write(tmp_path / "c.yaml", body)).storage_options() is None


def _s3_config(tmp_path: Path, allow_http_line: str) -> dict:
    body = (
        "lancedb_api_key: k\nlancedb_region: r\ngeneva_host: h\n"
        "s3_access_key: a\ns3_secret_key: s\ns3_endpoint: e\ns3_region: auto\n"
        f"{allow_http_line}"
    )
    return load_config(_write(tmp_path / "c.yaml", body)).storage_options()


def test_aws_allow_http_defaults_false(tmp_path: Path):
    assert _s3_config(tmp_path, "")["aws_allow_http"] == "false"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("aws_allow_http: true\n", "true"),  # native YAML bool
        ('aws_allow_http: "true"\n', "true"),  # quoted string
        ("aws_allow_http: false\n", "false"),
        ('aws_allow_http: "false"\n', "false"),  # quoted "false" is NOT truthy
    ],
)
def test_aws_allow_http_coercion(tmp_path: Path, line: str, expected: str):
    assert _s3_config(tmp_path, line)["aws_allow_http"] == expected


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
