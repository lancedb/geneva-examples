"""End-to-end smoke tests for the generated stage CLIs.

Each example step's ``uv run <name>`` command is a ``click.Command`` built from
its spec. These drive the commands through ``click``'s ``CliRunner`` in **local
mode** with the cluster boundary mocked (fake geneva + a FakeConn), so a
regression in the glue — resolve config → connect → build manifest → build
UDF(s) → backfill — fails fast without a cluster, GPU, or model weights.
"""

from __future__ import annotations

import importlib
from contextlib import nullcontext

import pytest
from _fakes import FakeConn, FakeTable
from click.testing import CliRunner

from geneva_examples.examples import cli


def _no_ray(mod, monkeypatch):
    """Replace the step module's runtime_session so no real local Ray starts."""
    monkeypatch.setattr(mod, "runtime_session", lambda *_a, **_k: nullcontext())


# (cli attr, step module, initial columns, expected backfilled columns, extra args)
STAGE_CASES = [
    (
        "lightweight",
        "geneva_examples.examples.images.lightweight",
        ["image"],
        {"file_size", "dimensions"},
        [],
    ),
    (
        "caption",
        "geneva_examples.examples.images.caption",
        ["image"],
        {"caption_blip"},
        [],
    ),
    (
        "embed",
        "geneva_examples.examples.images.embed",
        ["image"],
        {"embedding"},
        ["--no-search-demo"],
    ),
    (
        "frame_embed",
        "geneva_examples.examples.video.frame_embed",
        ["frame"],
        {"embedding"},
        [],
    ),
    (
        "frame_caption",
        "geneva_examples.examples.video.frame_caption",
        ["frame"],
        {"caption"},
        [],
    ),
    (
        "frame_openpose",
        "geneva_examples.examples.video.frame_openpose",
        ["frame"],
        {"pose"},
        [],
    ),
]


@pytest.mark.parametrize(
    "cli_attr,module_path,columns,expected,extra",
    STAGE_CASES,
    ids=[c[0] for c in STAGE_CASES],
)
def test_stage_cli_wires_backfill(
    monkeypatch: pytest.MonkeyPatch,
    fake_geneva: None,
    cli_attr: str,
    module_path: str,
    columns: list[str],
    expected: set[str],
    extra: list[str],
) -> None:
    mod = importlib.import_module(module_path)
    table = FakeTable(names=list(columns))
    monkeypatch.setattr(
        mod, "connect", lambda _cfg: FakeConn(table=table, is_remote=False)
    )
    _no_ray(mod, monkeypatch)

    result = CliRunner().invoke(
        getattr(cli, cli_attr),
        ["--mode", "local", "--schema-wait-sleep-s", "0", *extra],
    )

    assert result.exit_code == 0, result.output
    assert set(table.added) == expected
    assert set(table.backfilled) == expected


def test_pdf_chunk_stage_wires_backfill(monkeypatch: pytest.MonkeyPatch) -> None:
    # The PDF step reuses the *real* geneva.udfs.document UDFs, so it can't use
    # the fake-geneva harness — drive it with real geneva but connection mocked.
    from geneva_examples.examples.pdf import chunk as mod

    table = FakeTable(names=["doc_id", "pdf_bytes"])
    monkeypatch.setattr(
        mod, "connect", lambda _cfg: FakeConn(table=table, is_remote=False)
    )
    _no_ray(mod, monkeypatch)

    result = CliRunner().invoke(
        cli.chunk_pdfs, ["--mode", "local", "--schema-wait-sleep-s", "0"]
    )

    assert result.exit_code == 0, result.output
    assert list(table.added) == ["pages", "chunks"]
    assert table.backfilled == ["pages", "chunks"]
