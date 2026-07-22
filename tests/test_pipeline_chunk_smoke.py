"""Smoke tests for the generated video-chunking CLIs.

The chunk commands build a real geneva ``chunker`` and run it inside a
materialized view (``create_udtf_view`` + ``refresh``) rather than a column
backfill, so they exercise a different wiring path. Building the chunker is local
(no cluster), so these drive the commands with real geneva but the connection
boundary mocked.
"""

from __future__ import annotations

import importlib
import os
from contextlib import nullcontext

import pytest
from _fakes import FakeConn, FakeTable
from click.testing import CliRunner

from geneva_examples.examples import cli


@pytest.mark.parametrize(
    "cli_attr,module_path",
    [
        ("chunk_videos", "geneva_examples.examples.video.chunk"),
        ("chunk_videos_openvid", "geneva_examples.examples.video.chunk_openvid"),
    ],
)
def test_chunk_cli_creates_and_refreshes_view(
    monkeypatch: pytest.MonkeyPatch, cli_attr: str, module_path: str
) -> None:
    mod = importlib.import_module(module_path)
    table = FakeTable(names=["video_id", "chunk_id", "start_sec", "end_sec"])
    conn = FakeConn(table=table, is_remote=False)
    monkeypatch.setattr(mod, "connect", lambda _cfg: conn)
    monkeypatch.setattr(mod, "runtime_session", lambda *_a, **_k: nullcontext())

    result = CliRunner().invoke(getattr(cli, cli_attr), ["--mode", "local"])

    assert result.exit_code == 0, result.output
    # overwrite=True dropped the clips table, then the view was created under it.
    assert "video_clips" in conn.dropped
    assert "video_clips" in conn.created


# The worker-env transport keys the chunk CLI writes for the local UDF.
_ASSETS_ENV_KEYS = (
    "ASSETS_S3_ENDPOINT",
    "ASSETS_S3_ACCESS_KEY",
    "ASSETS_S3_SECRET_KEY",
    "ASSETS_S3_SCHEME",
    "ASSETS_S3_REGION",
)


def _chunk_external_conn(monkeypatch: pytest.MonkeyPatch) -> FakeConn:
    from geneva_examples.examples.video import chunk_external_video as mod

    conn = FakeConn(table=FakeTable(names=["video_id", "video_uri"]), is_remote=False)
    monkeypatch.setattr(mod, "connect", lambda _cfg: conn)
    monkeypatch.setattr(mod, "runtime_session", lambda *_a, **_k: nullcontext())
    # Preset stale values so (a) monkeypatch restores the ambient env after the
    # CLI's local-mode writes, and (b) the tests prove those writes win.
    for key in _ASSETS_ENV_KEYS:
        monkeypatch.setenv(key, "stale")
    return conn


def test_chunk_external_cli_creates_and_refreshes_view(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    conn = _chunk_external_conn(monkeypatch)

    result = CliRunner().invoke(
        cli.chunk_videos_external,
        [
            "--mode",
            "local",
            "--config",
            str(tmp_path / "missing.yaml"),
            "--video-endpoint",
            "http://minio.test:9000",
            "--video-access-key",
            "ak",
            "--video-secret-key",
            "sk",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "video_clips" in conn.dropped
    assert "video_clips" in conn.created
    # The resolved flag creds overwrote the stale ambient transport env (local
    # Ray workers share the driver env), endpoint peeled to host + scheme.
    assert os.environ["ASSETS_S3_ENDPOINT"] == "minio.test:9000"
    assert os.environ["ASSETS_S3_SCHEME"] == "http"
    assert os.environ["ASSETS_S3_ACCESS_KEY"] == "ak"


def test_chunk_external_cli_reads_creds_from_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    conn = _chunk_external_conn(monkeypatch)
    config = tmp_path / "config.yaml"
    config.write_text(
        "mode: local\n"
        "s3_access_key: storage-ak\n"  # storage block: must NOT be consulted
        "s3_secret_key: storage-sk\n"
        "s3_endpoint: http://storage-minio.test:9000\n"
        "s3_region: us-west-2\n"
        "assets_s3_access_key: cfg-ak\n"
        "assets_s3_secret_key: cfg-sk\n"
        "assets_s3_endpoint: http://cfg-minio.test:9000\n"
        "assets_s3_region: eu-central-1\n"
    )

    result = CliRunner().invoke(
        cli.chunk_videos_external, ["--mode", "local", "--config", str(config)]
    )

    assert result.exit_code == 0, result.output
    assert "video_clips" in conn.created
    # No --video-* flags: the assets_s3_* block supplied the creds (and the
    # storage s3_* block was ignored — separate credential sets).
    assert os.environ["ASSETS_S3_ACCESS_KEY"] == "cfg-ak"
    assert os.environ["ASSETS_S3_ENDPOINT"] == "cfg-minio.test:9000"
    assert os.environ["ASSETS_S3_SCHEME"] == "http"
    assert os.environ["ASSETS_S3_REGION"] == "eu-central-1"


def test_chunk_external_cli_threads_uri_column(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # geneva validates the chunker's input_columns against the view's source
    # projection server-side, so --uri-column must reach both or the refresh
    # fails. Record what the CLI hands to select() and create_udtf_view().
    from geneva_examples.examples.video import chunk_external_video as mod

    class _SelectRecordingTable(FakeTable):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.selected: list[list[str]] = []

        def select(self, columns=None, *args, **kwargs):
            if columns is not None:
                self.selected.append(list(columns))
            return super().select(columns, *args, **kwargs)

    class _ViewRecordingConn(FakeConn):
        view_udtf = None

        def create_udtf_view(self, name, source=None, udtf=None, **kwargs):
            self.view_udtf = udtf
            return super().create_udtf_view(name, source=source, udtf=udtf, **kwargs)

    table = _SelectRecordingTable(names=["video_id", "my_uri"])
    conn = _ViewRecordingConn(table=table, is_remote=False)
    monkeypatch.setattr(mod, "connect", lambda _cfg: conn)
    monkeypatch.setattr(mod, "runtime_session", lambda *_a, **_k: nullcontext())
    for key in _ASSETS_ENV_KEYS:
        monkeypatch.setenv(key, "stale")

    result = CliRunner().invoke(
        cli.chunk_videos_external,
        [
            "--mode",
            "local",
            "--config",
            str(tmp_path / "missing.yaml"),
            "--uri-column",
            "my_uri",
            "--video-endpoint",
            "http://minio.test:9000",
            "--video-access-key",
            "ak",
            "--video-secret-key",
            "sk",
        ],
    )

    assert result.exit_code == 0, result.output
    assert table.selected[0] == ["video_id", "my_uri"]  # source projection
    assert conn.view_udtf.input_columns == ["my_uri"]  # chunker declaration
    assert set(conn.view_udtf.input_columns) <= set(table.selected[0])


def test_chunk_external_cli_requires_video_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # Ambient ASSETS_S3_* is set ("stale") but must not satisfy the resolution —
    # it is the transport the CLI writes, never a driver-side input.
    conn = _chunk_external_conn(monkeypatch)

    result = CliRunner().invoke(
        cli.chunk_videos_external,
        ["--mode", "local", "--config", str(tmp_path / "missing.yaml")],
    )

    assert result.exit_code != 0
    assert "missing video-bucket credentials" in str(result.exception)
    assert "video_clips" not in conn.created  # failed before touching the table
