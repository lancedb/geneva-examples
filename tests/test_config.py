"""Tests for YAML config loading and mode resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from geneva_examples.core.config import (
    DEFAULT_DB_URI,
    DEFAULT_LOCAL_DB_PATH,
    load_config,
    normalize_db_uri,
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


# --- db_uri normalization ----------------------------------------------------


def test_normalize_db_uri_prepends_scheme_for_bare_enterprise_name(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        assert normalize_db_uri("tts", "enterprise") == "db://tts"
    # the correction is surfaced, not silent
    assert "no scheme" in caplog.text
    assert "db://tts" in caplog.text


def test_normalize_db_uri_leaves_existing_schemes_alone():
    for uri in ("db://tts", "s3://bucket/db", "gs://bucket/db", "az://container/db"):
        assert normalize_db_uri(uri, "enterprise") == uri


def test_normalize_db_uri_leaves_filesystem_paths_alone():
    # An explicit path is a deliberate on-disk database, not a typo.
    for uri in ("./scratch", "../scratch", "/tmp/scratch", "~/scratch"):
        assert normalize_db_uri(uri, "enterprise") == uri


def test_normalize_db_uri_is_a_noop_in_local_mode(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        assert normalize_db_uri("tts", "local") == "tts"
    assert caplog.text == ""  # local ignores db_uri; no warning to give


def test_normalize_db_uri_tolerates_blank():
    assert normalize_db_uri("", "enterprise") == ""
    assert normalize_db_uri("   ", "enterprise") == "   "


def test_load_config_normalizes_bare_db_uri_from_file(tmp_path: Path):
    cfg_path = _write(
        tmp_path / "c.yaml",
        "mode: enterprise\nlancedb_api_key: k\nlancedb_region: r\n"
        "geneva_host: http://h\ndb_uri: tts\n",
    )
    assert load_config(cfg_path).db_uri == "db://tts"


def test_load_config_db_uri_override_wins_and_is_normalized(tmp_path: Path):
    cfg_path = _write(
        tmp_path / "c.yaml",
        "mode: enterprise\nlancedb_api_key: k\nlancedb_region: r\n"
        "geneva_host: http://h\ndb_uri: db://from-file\n",
    )
    cfg = load_config(cfg_path, db_uri_override="smoke")
    assert cfg.db_uri == "db://smoke"


def test_load_config_db_uri_override_ignored_when_blank(tmp_path: Path):
    cfg_path = _write(
        tmp_path / "c.yaml",
        "mode: enterprise\nlancedb_api_key: k\nlancedb_region: r\n"
        "geneva_host: http://h\ndb_uri: db://from-file\n",
    )
    assert load_config(cfg_path, db_uri_override=None).db_uri == "db://from-file"


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


def test_azure_credentials_take_precedence_over_s3(tmp_path: Path):
    """Azure wins when both sets are present — the account-less az:// root
    URI can't be resolved without the account named here."""
    body = (
        "lancedb_api_key: k\nlancedb_region: r\ngeneva_host: h\n"
        "s3_access_key: a\ns3_secret_key: s\ns3_endpoint: e\ns3_region: auto\n"
        "azure_account_name: acct\nazure_account_key: secret\n"
    )
    opts = load_config(_write(tmp_path / "c.yaml", body)).storage_options()
    assert opts == {
        "azure_storage_account_name": "acct",
        "azure_storage_account_key": "secret",
    }


def test_azure_requires_both_name_and_key(tmp_path: Path):
    body = (
        "lancedb_api_key: k\nlancedb_region: r\ngeneva_host: h\n"
        "azure_account_name: acct\n"  # key missing -> not usable
    )
    assert load_config(_write(tmp_path / "c.yaml", body)).storage_options() is None


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
