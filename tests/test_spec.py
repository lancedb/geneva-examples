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


def test_build_command_db_uri_override_reaches_config_normalized(tmp_path):
    """`--db-uri` flows through resolve_config and keeps its db:// scheme.

    Regression guard: the override used to be applied by assigning to the
    returned Config, which bypassed normalization — a bare name then silently
    became an on-disk database instead of a cluster connection.
    """
    calls: dict = {}

    def run(cfg) -> None:
        calls["cfg"] = cfg

    step = Step("demo", "Demo", "desc", run)
    cmd = build_command(Example("x", "X", "d", "image", (step,)), step)

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "mode: enterprise\nlancedb_api_key: k\nlancedb_region: r\n"
        "geneva_host: http://h\ndb_uri: db://from-file\n"
    )
    result = CliRunner().invoke(
        cmd, ["--config", str(config_file), "--db-uri", "scratch"]
    )

    assert result.exit_code == 0, result.output
    assert calls["cfg"].db_uri == "db://scratch"  # overrode the file, kept scheme


def test_build_command_without_db_uri_keeps_the_file_value(tmp_path):
    calls: dict = {}

    def run(cfg) -> None:
        calls["cfg"] = cfg

    step = Step("demo", "Demo", "desc", run)
    cmd = build_command(Example("x", "X", "d", "image", (step,)), step)

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "mode: enterprise\nlancedb_api_key: k\nlancedb_region: r\n"
        "geneva_host: http://h\ndb_uri: db://from-file\n"
    )
    result = CliRunner().invoke(cmd, ["--config", str(config_file)])

    assert result.exit_code == 0, result.output
    assert calls["cfg"].db_uri == "db://from-file"


def test_build_command_range_validation():
    def run(cfg, *, n: int = 1) -> None: ...

    step = Step("d", "D", "x", run, params=(Param("n", int, 1, "n", min=1, max=5),))
    cmd = build_command(Example("x", "X", "d", "image", (step,)), step)
    bad = CliRunner().invoke(cmd, ["--mode", "local", "--n", "99"])
    assert bad.exit_code != 0  # out of range


def test_build_command_choice_param_rejects_unknown_value():
    """A `choices` param becomes a click.Choice, so bad values fail at parse."""
    calls: dict = {}

    def run(cfg, *, fmt: str = "wav") -> None:
        calls["fmt"] = fmt

    step = Step(
        "demo",
        "Demo",
        "desc",
        run,
        params=(Param("fmt", str, "wav", "format", choices=("wav", "mp3")),),
    )
    cmd = build_command(Example("x", "X", "d", "image", (step,)), step)

    ok = CliRunner().invoke(cmd, ["--mode", "local", "--fmt", "mp3"])
    assert ok.exit_code == 0, ok.output
    assert calls["fmt"] == "mp3"

    bad = CliRunner().invoke(cmd, ["--mode", "local", "--fmt", "flac"])
    assert bad.exit_code != 0
    assert "flac" in bad.output


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
