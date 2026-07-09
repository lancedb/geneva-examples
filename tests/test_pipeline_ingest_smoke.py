"""Smoke tests for the generated ingest CLIs + the cleanup op.

Drive each command through ``CliRunner`` in local mode with the cluster boundary
and the data-source loaders mocked (no network, HF, or Ray), so a regression in
the wiring — resolve config → connect → create_table → add, or the cleanup drop
loop — fails fast. These commands are excluded from the coverage gate.
"""

from __future__ import annotations

import pytest
from _fakes import FakeConn, FakeTable
from click.testing import CliRunner
from typer.testing import CliRunner as TyperCliRunner

from geneva_examples.examples import cli


def test_ingest_images_creates_and_adds(monkeypatch: pytest.MonkeyPatch) -> None:
    from geneva_examples.examples.images import ingest as mod

    conn = FakeConn(table=FakeTable(names=["image_id", "label"]))
    monkeypatch.setattr(mod, "connect", lambda _cfg: conn)
    monkeypatch.setattr(
        "geneva_examples.core.utils.images.load_hf_image_batches",
        lambda **_kw: [[{"a": 1}], [{"a": 2}], [{"a": 3}]],
    )

    result = CliRunner().invoke(cli.ingest_images, ["--mode", "local"])

    assert result.exit_code == 0, result.output
    assert "images" in conn.created  # created the default table
    assert "images" in conn.dropped  # overwrite=True dropped it first
    assert len(conn.created["images"].adds) == 2  # batches[1:] appended


def test_ingest_images_raises_when_no_data(monkeypatch: pytest.MonkeyPatch) -> None:
    from geneva_examples.examples.images import ingest as mod

    conn = FakeConn(table=FakeTable())
    monkeypatch.setattr(mod, "connect", lambda _cfg: conn)
    monkeypatch.setattr(
        "geneva_examples.core.utils.images.load_hf_image_batches", lambda **_kw: []
    )

    result = CliRunner().invoke(cli.ingest_images, ["--mode", "local"])

    assert result.exit_code != 0
    assert "images" not in conn.created


def test_ingest_videos_creates_and_adds(monkeypatch: pytest.MonkeyPatch) -> None:
    from geneva_examples.examples.video import ingest as mod

    conn = FakeConn(table=FakeTable(names=["video_id", "video"]))
    monkeypatch.setattr(mod, "connect", lambda _cfg: conn)
    monkeypatch.setattr(
        "geneva_examples.core.utils.videos.download_video_batches",
        lambda *_a, **_k: [[{"v": 1}], [{"v": 2}]],
    )

    result = CliRunner().invoke(cli.ingest_videos, ["--mode", "local"])

    assert result.exit_code == 0, result.output
    assert "videos" in conn.created
    assert len(conn.created["videos"].adds) == 1


def test_ingest_pdfs_creates_and_adds(monkeypatch: pytest.MonkeyPatch) -> None:
    from geneva_examples.examples.pdf import ingest as mod

    conn = FakeConn(table=FakeTable(names=["doc_id", "pdf_bytes"]))
    monkeypatch.setattr(mod, "connect", lambda _cfg: conn)
    monkeypatch.setattr(
        "geneva_examples.core.utils.pdfs.load_pdf_batches",
        lambda *_a, **_k: [[{"d": 1}]],
    )

    result = CliRunner().invoke(
        cli.ingest_pdfs, ["--mode", "local", "--pdf-dir", "/tmp/none"]
    )

    assert result.exit_code == 0, result.output
    assert "pdfs" in conn.created


def test_cleanup_drops_tables_and_mv_siblings(monkeypatch: pytest.MonkeyPatch) -> None:
    from geneva_examples.ops import cleanup

    conn = FakeConn()
    monkeypatch.setattr(cleanup, "connect", lambda _cfg: conn)

    result = TyperCliRunner().invoke(
        cleanup.app, ["--yes", "--mode", "local", "--pdfs-table", "pdfs"]
    )

    assert result.exit_code == 0, result.output
    assert conn.dropped == [
        "videos",
        "videos_mv",
        "video_clips",
        "video_clips_mv",
        "pdfs",
    ]
