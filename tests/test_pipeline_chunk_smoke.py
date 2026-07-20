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
_VIDEO_ENV_KEYS = (
    "VIDEO_S3_ENDPOINT",
    "VIDEO_S3_ACCESS_KEY",
    "VIDEO_S3_SECRET_KEY",
    "VIDEO_S3_SCHEME",
    "VIDEO_S3_REGION",
)


def _chunk_external_conn(monkeypatch: pytest.MonkeyPatch) -> FakeConn:
    from geneva_examples.examples.video import chunk_external_video as mod

    conn = FakeConn(table=FakeTable(names=["video_id", "video_uri"]), is_remote=False)
    monkeypatch.setattr(mod, "connect", lambda _cfg: conn)
    monkeypatch.setattr(mod, "runtime_session", lambda *_a, **_k: nullcontext())
    # Preset stale values so (a) monkeypatch restores the ambient env after the
    # CLI's local-mode writes, and (b) the tests prove those writes win.
    for key in _VIDEO_ENV_KEYS:
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
    assert os.environ["VIDEO_S3_ENDPOINT"] == "minio.test:9000"
    assert os.environ["VIDEO_S3_SCHEME"] == "http"
    assert os.environ["VIDEO_S3_ACCESS_KEY"] == "ak"


def test_chunk_external_cli_reads_creds_from_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    conn = _chunk_external_conn(monkeypatch)
    config = tmp_path / "config.yaml"
    config.write_text(
        "mode: local\n"
        "s3_access_key: cfg-ak\n"
        "s3_secret_key: cfg-sk\n"
        "s3_endpoint: http://cfg-minio.test:9000\n"
        "s3_region: eu-central-1\n"
    )

    result = CliRunner().invoke(
        cli.chunk_videos_external, ["--mode", "local", "--config", str(config)]
    )

    assert result.exit_code == 0, result.output
    assert "video_clips" in conn.created
    # No --video-* flags: the config's s3_* storage block supplied the creds.
    assert os.environ["VIDEO_S3_ACCESS_KEY"] == "cfg-ak"
    assert os.environ["VIDEO_S3_ENDPOINT"] == "cfg-minio.test:9000"
    assert os.environ["VIDEO_S3_SCHEME"] == "http"
    assert os.environ["VIDEO_S3_REGION"] == "eu-central-1"


def test_chunk_external_cli_requires_video_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # Ambient VIDEO_S3_* is set ("stale") but must not satisfy the resolution —
    # it is the transport the CLI writes, never a driver-side input.
    conn = _chunk_external_conn(monkeypatch)

    result = CliRunner().invoke(
        cli.chunk_videos_external,
        ["--mode", "local", "--config", str(tmp_path / "missing.yaml")],
    )

    assert result.exit_code != 0
    assert "missing video-bucket credentials" in str(result.exception)
    assert "video_clips" not in conn.created  # failed before touching the table
