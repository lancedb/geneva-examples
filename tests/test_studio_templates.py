"""Tests for UDF Studio starter templates."""

from __future__ import annotations

from pathlib import Path

import pytest

from geneva_examples.apps.studio import runner, samples
from geneva_examples.apps.studio.templates import DEFAULT_TEMPLATE, TEMPLATES


def test_default_template_exists():
    assert DEFAULT_TEMPLATE in TEMPLATES
    assert TEMPLATES


@pytest.mark.parametrize("name", list(TEMPLATES))
def test_template_is_well_formed_and_compiles(name: str):
    tpl = TEMPLATES[name]
    assert tpl["kind"] in {"udf", "chunker"}
    assert tpl["modality"] in samples.MODALITIES
    compile(tpl["code"], f"<template:{name}>", "exec")  # must be valid Python


@pytest.mark.parametrize(
    "name",
    ["image · dimensions (w×h)", "image · file size (bytes)"],
)
def test_image_templates_run(name: str, data_dir: Path):
    values = samples.sample(data_dir, "image", n=2)["values"]
    res = runner.run("udf", TEMPLATES[name]["code"], values)
    assert res["ok"]
    assert all(r["error"] == "" for r in res["rows"])


def test_text_template_runs():
    res = runner.run("udf", TEMPLATES["text · word + char count"]["code"], ["hi there"])
    assert res["ok"]
    assert res["rows"][0]["output"] == "{'chars': 8, 'words': 2}"
