"""Tests for YAML config loading and mode resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from geneva_examples.core.config import (
    DEFAULT_DB_URI,
    DEFAULT_LOCAL_DB_PATH,
    load_config,
    resolve_mode,
)


def _write(path: Path, body: str) -> Path:
    path.write_text(body)
    return path


# --- mode resolution ---------------------------------------------------------


def test_resolve_mode_override_wins():
    assert resolve_mode("enterprise", {"mode": "local"}) == "enterprise"
    assert resolve_mode("local", {"geneva_host": "h"}) == "local"


def test_resolve_mode_from_config_key():
    assert resolve_mode(None, {"mode": "enterprise"}) == "enterprise"
    assert resolve_mode(None, {"mode": "local"}) == "local"


def test_resolve_mode_infers_enterprise_from_geneva_host():
    assert resolve_mode(None, {"geneva_host": "http://h"}) == "enterprise"


def test_resolve_mode_defaults_to_local():
    assert resolve_mode(None, {}) == "local"


def test_resolve_mode_invalid_raises():
    with pytest.raises(RuntimeError, match="invalid mode"):
        resolve_mode("bogus", {})


# --- local mode --------------------------------------------------------------


def test_missing_file_defaults_to_local(tmp_path: Path):
    cfg = load_config(tmp_path / "absent.yaml")
    assert cfg.mode == "local"
    assert cfg.is_local
    assert cfg.local_db_path == DEFAULT_LOCAL_DB_PATH
    assert cfg.lancedb_api_key is None


def test_local_mode_requires_no_secrets(tmp_path: Path):
    cfg = load_config(_write(tmp_path / "c.yaml", "mode: local\n"))
    assert cfg.is_local
    assert cfg.storage_options() is None


def test_local_db_path_override(tmp_path: Path):
    cfg = load_config(
        _write(tmp_path / "c.yaml", "mode: local\nlocal_db_path: /tmp/mydb\n")
    )
    assert cfg.local_db_path == "/tmp/mydb"


def test_mode_override_forces_local_despite_geneva_host(tmp_path: Path):
    body = "lancedb_api_key: k\nlancedb_region: r\ngeneva_host: h\n"
    cfg = load_config(_write(tmp_path / "c.yaml", body), mode_override="local")
    assert cfg.is_local


# --- enterprise mode ---------------------------------------------------------


def test_load_minimal_applies_defaults(tmp_path: Path):
    cfg = load_config(
        _write(
            tmp_path / "c.yaml",
            "lancedb_api_key: key\nlancedb_region: us-east-1\ngeneva_host: host:80\n",
        )
    )
    assert cfg.mode == "enterprise"  # inferred from geneva_host
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


def test_enterprise_missing_required_field_raises(tmp_path: Path):
    # mode is explicitly enterprise but the cloud fields are absent.
    with pytest.raises(RuntimeError, match="missing required config"):
        load_config(_write(tmp_path / "c.yaml", "mode: enterprise\n"))


def test_enterprise_missing_file_raises(tmp_path: Path):
    with pytest.raises(RuntimeError, match="config file not found"):
        load_config(tmp_path / "absent.yaml", mode_override="enterprise")


def test_load_config_defaults_to_cwd_config_yaml(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path / "config.yaml",
        "lancedb_api_key: k\nlancedb_region: r\ngeneva_host: h\n",
    )
    cfg = load_config()  # no path -> ./config.yaml
    assert cfg.lancedb_api_key == "k"
    assert cfg.mode == "enterprise"
