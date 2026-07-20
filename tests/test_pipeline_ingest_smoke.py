"""Smoke tests for the generated ingest CLIs + the cleanup op.

Drive each command through ``CliRunner`` in local mode with the cluster boundary
and the data-source loaders mocked (no network, HF, or Ray), so a regression in
the wiring — resolve config → connect → create_table → add, or the cleanup drop
loop — fails fast. These commands are excluded from the coverage gate.
"""

from __future__ import annotations

import types

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


# --- ingest-videos-external ---------------------------------------------------


class _RecordingConn(FakeConn):
    """FakeConn that also captures the Arrow table handed to ``create_table``."""

    table_data = None

    def create_table(self, name, data=None, **kwargs):
        self.table_data = data
        return super().create_table(name, data=data, **kwargs)


class _FakeVideoBucketFS:
    """``pyarrow.fs.S3FileSystem`` stand-in listing a fixed set of objects."""

    def __init__(self, infos):
        self.infos = infos
        self.selectors = []

    def get_file_info(self, selector):
        self.selectors.append(selector)
        return list(self.infos)


def _bucket_file(path: str, size: int):
    import pyarrow.fs as pafs

    return types.SimpleNamespace(path=path, size=size, type=pafs.FileType.File)


def _bucket_dir(path: str):
    import pyarrow.fs as pafs

    return types.SimpleNamespace(path=path, size=0, type=pafs.FileType.Directory)


def _invoke_ingest_external(monkeypatch, infos, args):
    from geneva_examples.examples.video import ingest_external_refs as mod

    conn = _RecordingConn(table=FakeTable(names=["video_id", "video_uri", "size_mb"]))
    monkeypatch.setattr(mod, "connect", lambda _cfg: conn)
    fs = _FakeVideoBucketFS(infos)
    constructed: list[dict] = []

    def _factory(**kwargs):
        constructed.append(kwargs)
        return fs

    monkeypatch.setattr("pyarrow.fs.S3FileSystem", _factory)
    result = CliRunner().invoke(
        cli.ingest_videos_external,
        [
            "--mode",
            "local",
            "--video-bucket",
            "vids",
            "--video-endpoint",
            "http://minio.test:9000",
            "--video-access-key",
            "ak",
            "--video-secret-key",
            "sk",
            *args,
        ],
    )
    return result, conn, fs, constructed


def test_ingest_videos_external_registers_reference_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    infos = [
        _bucket_file("vids/big.mp4", 3_000_000),
        _bucket_file("vids/small.mp4", 1_000_000),
        _bucket_file("vids/mid.mp4", 2_000_000),
        _bucket_file("vids/notes.txt", 10),  # suffix-filtered out
        _bucket_dir("vids/subdir"),  # non-file entries ignored
    ]
    result, conn, fs, constructed = _invoke_ingest_external(
        monkeypatch, infos, ["--limit", "2"]
    )

    assert result.exit_code == 0, result.output
    assert "videos" in conn.dropped  # overwrite=True
    assert "videos" in conn.created
    # The endpoint URL was peeled into a bare host + scheme for pyarrow.
    assert constructed[0]["endpoint_override"] == "minio.test:9000"
    assert constructed[0]["scheme"] == "http"
    assert fs.selectors[0].base_dir == "vids"
    assert fs.selectors[0].recursive is True
    # Reference-only rows: smallest two, id = basename sans suffix, s3:// URI.
    data = conn.table_data
    assert data.column("video_id").to_pylist() == ["small", "mid"]
    assert data.column("video_uri").to_pylist() == [
        "s3://vids/small.mp4",
        "s3://vids/mid.mp4",
    ]
    assert data.column("size_mb").to_pylist() == [1.0, 2.0]


def test_ingest_videos_external_lists_under_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    infos = [_bucket_file("vids/raw/v.mp4", 1_000_000)]
    result, _conn, fs, _ = _invoke_ingest_external(
        monkeypatch, infos, ["--prefix", "raw"]
    )
    assert result.exit_code == 0, result.output
    assert fs.selectors[0].base_dir == "vids/raw"


def test_ingest_videos_external_stride_sample_spans_sizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    infos = [_bucket_file(f"vids/v{i}.mp4", i * 1_000_000) for i in range(1, 11)]
    result, conn, _fs, _ = _invoke_ingest_external(
        monkeypatch, infos, ["--limit", "3", "--sample", "stride"]
    )
    assert result.exit_code == 0, result.output
    # Systematic sample over the size-sorted corpus: ranks 0, 3, 7 of 10 —
    # spanning the distribution instead of the three smallest.
    assert conn.table_data.column("size_mb").to_pylist() == [1.0, 4.0, 8.0]


def test_ingest_videos_external_rejects_unknown_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    infos = [_bucket_file("vids/v.mp4", 1_000_000)]
    result, conn, _fs, _ = _invoke_ingest_external(
        monkeypatch, infos, ["--sample", "bogus"]
    )
    assert result.exit_code != 0
    assert "unknown --sample" in str(result.exception)
    assert "videos" not in conn.created


def test_ingest_videos_external_errors_when_nothing_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, conn, _fs, _ = _invoke_ingest_external(
        monkeypatch, [_bucket_file("vids/readme.txt", 10)], []
    )
    assert result.exit_code != 0
    assert "no .mp4 objects" in str(result.exception)
    assert "videos" not in conn.created
