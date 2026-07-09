"""End-to-end smoke tests for the stage CLIs.

The stage CLIs are excluded from the coverage gate because their bodies open a
live Geneva/Ray connection. Their unit-tested pieces (the ``backfill_column``
runner, the UDF manifests) are covered elsewhere, but nothing exercises the
*wiring* that glues them together: load config -> connect -> build manifest ->
build UDF(s) -> backfill each column. These tests drive each mockable stage through
``typer``'s ``CliRunner`` with the cluster boundary mocked (see ``tests/_fakes.py``),
so a regression in that glue fails fast without a cluster, GPU, or model weights.

The ``embed`` stage is driven with ``--no-search-demo`` (its own test below): the
optional post-backfill search demo imports ``open_clip``+``torch`` on the driver,
but the backfill wiring itself doesn't, so the flag lets it run mocked.
"""

from __future__ import annotations

import importlib
import types

import pytest
from _fakes import FakeConn, FakeTable
from typer.testing import CliRunner

# (module path, the column(s) the stage should add + backfill). Each builds its UDF
# via a factory that imports only geneva+pyarrow at build time, so the fake geneva
# from the `fake_geneva` fixture is enough.
STAGE_CASES = [
    ("geneva_examples.pipeline.stages.lightweight", {"file_size", "dimensions"}),
    ("geneva_examples.pipeline.stages.captions", {"caption_blip", "caption_blip_v2"}),
    ("geneva_examples.pipeline.stages.frame_embed", {"embedding"}),
    ("geneva_examples.pipeline.stages.frame_caption", {"caption"}),
    ("geneva_examples.pipeline.stages.frame_openpose", {"pose"}),
]


@pytest.mark.parametrize(
    "module_path,expected", STAGE_CASES, ids=lambda v: v if isinstance(v, str) else ""
)
def test_stage_cli_wires_backfill(
    monkeypatch: pytest.MonkeyPatch,
    fake_geneva: None,
    module_path: str,
    expected: set[str],
) -> None:
    mod = importlib.import_module(module_path)
    table = FakeTable(names=["id"])

    cfg = types.SimpleNamespace(db_uri="db://test", table_name="images")
    monkeypatch.setattr(mod, "load_config", lambda _config: cfg)
    monkeypatch.setattr(mod, "connect", lambda _cfg: FakeConn(table=table))

    result = CliRunner().invoke(mod.app, ["--schema-wait-sleep-s", "0"])

    assert result.exit_code == 0, result.output
    # Every expected feature column was added and backfilled via the shared runner.
    assert set(table.added) == expected
    assert set(table.backfilled) == expected


def test_embed_stage_wires_backfill(
    monkeypatch: pytest.MonkeyPatch, fake_geneva: None
) -> None:
    # --no-search-demo skips the open_clip/torch driver demo, leaving just the
    # backfill wiring to exercise.
    mod = importlib.import_module("geneva_examples.pipeline.stages.embeddings")
    table = FakeTable(names=["id"])

    cfg = types.SimpleNamespace(db_uri="db://test")
    monkeypatch.setattr(mod, "load_config", lambda _config: cfg)
    monkeypatch.setattr(mod, "connect", lambda _cfg: FakeConn(table=table))

    result = CliRunner().invoke(
        mod.app, ["--no-search-demo", "--schema-wait-sleep-s", "0"]
    )

    assert result.exit_code == 0, result.output
    assert set(table.added) == {"embedding"}
    assert set(table.backfilled) == {"embedding"}


def test_pdf_chunk_stage_wires_backfill(monkeypatch: pytest.MonkeyPatch) -> None:
    # The PDF stage reuses the *real* geneva.udfs.document UDFs (building a pip
    # manifest is local, no cluster), so it can't use the fake-geneva harness
    # above — drive it with real geneva but the connection boundary mocked.
    mod = importlib.import_module("geneva_examples.pipeline.stages.pdf_chunks")
    table = FakeTable(names=["doc_id", "pdf_bytes"])

    cfg = types.SimpleNamespace(db_uri="db://test", table_name="pdfs")
    monkeypatch.setattr(mod, "load_config", lambda _config: cfg)
    monkeypatch.setattr(mod, "connect", lambda _cfg: FakeConn(table=table))

    result = CliRunner().invoke(mod.app, ["--schema-wait-sleep-s", "0"])

    assert result.exit_code == 0, result.output
    # `pages` then `chunks` are both added and backfilled, in that order.
    assert list(table.added) == ["pages", "chunks"]
    assert table.backfilled == ["pages", "chunks"]
