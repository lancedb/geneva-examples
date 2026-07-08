"""End-to-end smoke tests for the ingest + cleanup CLIs.

Like the stage smoke tests, these drive each CLI through ``CliRunner`` with the
cluster boundary and the data-source loaders mocked (no network, HF, or Ray), so
a regression in the wiring — load config -> connect -> create_table -> add, or
the cleanup drop loop — fails fast. These CLIs are excluded from the coverage
gate (they open a live connection), so this is the only automated guard on them.
"""

from __future__ import annotations

import types

import pytest
from _fakes import FakeConn, FakeTable
from typer.testing import CliRunner


def _cfg() -> types.SimpleNamespace:
    return types.SimpleNamespace(db_uri="db://test")


def test_ingest_images_creates_and_adds(
    monkeypatch: pytest.MonkeyPatch, fake_geneva: None
) -> None:
    from geneva_examples.pipeline import ingest_images

    conn = FakeConn(table=FakeTable(names=["image_id", "label"]))
    monkeypatch.setattr(ingest_images, "load_config", lambda _c: _cfg())
    monkeypatch.setattr(ingest_images, "connect", lambda _cfg: conn)
    # Three batches: the first creates the table, the rest are add()ed.
    monkeypatch.setattr(
        "geneva_examples.core.utils.images.load_hf_image_batches",
        lambda **_kw: [[{"a": 1}], [{"a": 2}], [{"a": 3}]],
    )

    result = CliRunner().invoke(ingest_images.app, [])

    assert result.exit_code == 0, result.output
    assert "images" in conn.created  # created the default table
    assert "images" in conn.dropped  # overwrite=True dropped it first
    assert len(conn.created["images"].adds) == 2  # batches[1:] appended


def test_ingest_images_raises_when_no_data(
    monkeypatch: pytest.MonkeyPatch, fake_geneva: None
) -> None:
    from geneva_examples.pipeline import ingest_images

    conn = FakeConn(table=FakeTable())
    monkeypatch.setattr(ingest_images, "load_config", lambda _c: _cfg())
    monkeypatch.setattr(ingest_images, "connect", lambda _cfg: conn)
    monkeypatch.setattr(
        "geneva_examples.core.utils.images.load_hf_image_batches",
        lambda **_kw: [],
    )

    result = CliRunner().invoke(ingest_images.app, [])

    assert result.exit_code != 0
    assert "images" not in conn.created


def test_ingest_videos_creates_and_adds(
    monkeypatch: pytest.MonkeyPatch, fake_geneva: None
) -> None:
    from geneva_examples.pipeline import ingest_videos

    conn = FakeConn(table=FakeTable(names=["video_id", "video"]))
    monkeypatch.setattr(ingest_videos, "load_config", lambda _c: _cfg())
    monkeypatch.setattr(ingest_videos, "connect", lambda _cfg: conn)
    monkeypatch.setattr(
        "geneva_examples.core.utils.videos.download_video_batches",
        lambda *_a, **_k: [[{"v": 1}], [{"v": 2}]],
    )

    result = CliRunner().invoke(ingest_videos.app, [])

    assert result.exit_code == 0, result.output
    assert "videos" in conn.created
    assert len(conn.created["videos"].adds) == 1


def test_ingest_pdfs_creates_and_adds(
    monkeypatch: pytest.MonkeyPatch, fake_geneva: None
) -> None:
    from geneva_examples.pipeline import ingest_pdfs

    conn = FakeConn(table=FakeTable(names=["doc_id", "pdf_bytes"]))
    monkeypatch.setattr(ingest_pdfs, "load_config", lambda _c: _cfg())
    monkeypatch.setattr(ingest_pdfs, "connect", lambda _cfg: conn)
    monkeypatch.setattr(
        "geneva_examples.core.utils.pdfs.load_pdf_batches",
        lambda *_a, **_k: [[{"d": 1}]],
    )

    result = CliRunner().invoke(ingest_pdfs.app, ["--pdf-dir", "/tmp/none"])

    assert result.exit_code == 0, result.output
    assert "pdfs" in conn.created


def test_cleanup_drops_tables_and_mv_siblings(
    monkeypatch: pytest.MonkeyPatch, fake_geneva: None
) -> None:
    from geneva_examples.pipeline import cleanup

    conn = FakeConn()
    monkeypatch.setattr(cleanup, "load_config", lambda _c: _cfg())
    monkeypatch.setattr(cleanup, "connect", lambda _cfg: conn)

    result = CliRunner().invoke(cleanup.app, ["--yes", "--pdfs-table", "pdfs"])

    assert result.exit_code == 0, result.output
    assert conn.dropped == [
        "videos",
        "videos_mv",
        "video_clips",
        "video_clips_mv",
        "pdfs",
    ]
