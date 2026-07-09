"""End-to-end smoke tests for the video-chunking CLIs.

The chunk CLIs build a real geneva ``chunker`` and run it inside a materialized
view (``create_udtf_view`` + ``refresh``) rather than a column backfill, so they
exercise a different wiring path than the stage CLIs. Building the chunker is
local (no cluster), so — like the PDF-chunk test — these drive the CLIs with
real geneva but the connection boundary mocked (see ``tests/_fakes.py``).
"""

from __future__ import annotations

import importlib
import types

import pytest
from _fakes import FakeConn, FakeTable
from typer.testing import CliRunner


@pytest.mark.parametrize(
    "module_path",
    [
        "geneva_examples.pipeline.chunk_videos",
        "geneva_examples.pipeline.chunk_videos_openvid",
    ],
)
def test_chunk_cli_creates_and_refreshes_view(
    monkeypatch: pytest.MonkeyPatch, module_path: str
) -> None:
    mod = importlib.import_module(module_path)
    table = FakeTable(names=["video_id", "chunk_id", "start_sec", "end_sec"])
    conn = FakeConn(table=table)

    cfg = types.SimpleNamespace(db_uri="db://test", hf_token=None)
    monkeypatch.setattr(mod, "load_config", lambda _config: cfg)
    monkeypatch.setattr(mod, "connect", lambda _cfg: conn)

    result = CliRunner().invoke(mod.app, [])

    assert result.exit_code == 0, result.output
    # overwrite=True dropped the clips table, then the view was created under it.
    assert "video_clips" in conn.dropped
    assert "video_clips" in conn.created
