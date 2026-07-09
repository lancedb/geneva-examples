"""Tests for the example spec model + CLI generation."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from geneva_examples.core.config import Config
from geneva_examples.core.spec import (
    Example,
    Param,
    Step,
    _annotation_type,
    build_command,
    params_from_signature,
)


def test_annotation_type_handles_strings_and_optionals():
    assert _annotation_type("int") is int
    assert _annotation_type("float | None") is float
    assert _annotation_type("str") is str
    assert _annotation_type(bool) is bool
    assert _annotation_type("something_weird") is str  # safe fallback


def test_params_from_signature_derives_name_type_default():
    def run(cfg, *, a: int = 1, b: str = "x", c: float | None = None) -> None: ...

    params = {p.name: p for p in params_from_signature(run, help={"a": "the a"})}
    assert set(params) == {"a", "b", "c"}
    assert params["a"].type is int and params["a"].default == 1
    assert params["a"].help == "the a"
    assert params["b"].help == "b"  # humanized fallback
    assert params["c"].type is float and params["c"].default is None


def test_build_command_parses_and_calls_run():
    calls: dict = {}

    def run(cfg, *, count: int = 3, flag: bool = False, name: str = "hi") -> None:
        calls.update(cfg=cfg, count=count, flag=flag, name=name)

    step = Step(
        "demo",
        "Demo",
        "desc",
        run,
        params=(
            Param("count", int, 3, "n", min=0),
            Param("flag", bool, False, "f"),
            Param("name", str, "hi", "nm"),
        ),
    )
    cmd = build_command(Example("x", "X", "d", "image", (step,)), step)

    result = CliRunner().invoke(
        cmd, ["--mode", "local", "--count", "7", "--flag", "--name", "bob"]
    )
    assert result.exit_code == 0, result.output
    assert calls["count"] == 7 and calls["flag"] is True and calls["name"] == "bob"
    assert isinstance(calls["cfg"], Config) and calls["cfg"].is_local


def test_build_command_range_validation():
    def run(cfg, *, n: int = 1) -> None: ...

    step = Step("d", "D", "x", run, params=(Param("n", int, 1, "n", min=1, max=5),))
    cmd = build_command(Example("x", "X", "d", "image", (step,)), step)
    bad = CliRunner().invoke(cmd, ["--mode", "local", "--n", "99"])
    assert bad.exit_code != 0  # out of range


def test_build_command_help_shows_description():
    def run(cfg) -> None: ...

    step = Step("demo", "Demo", "A helpful description here.", run)
    result = CliRunner().invoke(
        build_command(Example("x", "X", "d", "image", (step,)), step), ["--help"]
    )
    assert "A helpful description here." in result.output


def test_example_step_lookup():
    step = Step("k", "K", "d", lambda cfg: None)
    ex = Example("x", "X", "d", "image", (step,))
    assert ex.step("k") is step
    with pytest.raises(KeyError):
        ex.step("missing")
