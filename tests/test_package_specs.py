"""Tests for the remote-runtime package-spec resolver."""

from __future__ import annotations

import pytest

from geneva_examples.core import package_specs
from geneva_examples.core.package_specs import _default_env_var, package_spec


def test_spec_reads_installed_version():
    # pytest is always installed in the test env; the spec pins its exact version.
    spec = package_spec("pytest")
    assert spec.startswith("pytest==")
    assert spec.split("==", 1)[1]  # a non-empty version


def test_env_override_wins_verbatim(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PYTEST_PACKAGE_SPEC", "pytest>=1,<99")
    assert package_spec("pytest") == "pytest>=1,<99"


def test_default_env_var_normalizes_hyphens_and_dots():
    # Hyphens/dots are illegal in env-var names -> normalized to underscores.
    assert _default_env_var("open-clip-torch") == "OPEN_CLIP_TORCH_PACKAGE_SPEC"
    assert _default_env_var("ruamel.yaml") == "RUAMEL_YAML_PACKAGE_SPEC"
    assert _default_env_var("numpy") == "NUMPY_PACKAGE_SPEC"


def test_hyphenated_override_key_is_usable(monkeypatch: pytest.MonkeyPatch):
    # The derived key for a hyphenated package resolves as a real env var.
    monkeypatch.setattr(package_specs, "version", lambda _p: "1.2.3")
    monkeypatch.setenv("OPEN_CLIP_TORCH_PACKAGE_SPEC", "open-clip-torch==9.9.9")
    assert package_spec("open-clip-torch") == "open-clip-torch==9.9.9"
