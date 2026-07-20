"""Smoke tests for the generated video-chunking CLIs.

The chunk commands build a real geneva ``chunker`` and run it inside a
materialized view (``create_udtf_view`` + ``refresh``) rather than a column
backfill, so they exercise a different wiring path. Building the chunker is local
(no cluster), so these drive the commands with real geneva but the connection
boundary mocked.
"""

from __future__ import annotations

import importlib
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


# All five worker-env keys, preset so the local-mode ``os.environ.setdefault``
# pass is a no-op (monkeypatch can then restore the ambient environment).
_VIDEO_ENV = {
    "VIDEO_S3_ENDPOINT": "http://minio.test:9000",
    "VIDEO_S3_ACCESS_KEY": "ak",
    "VIDEO_S3_SECRET_KEY": "sk",
    "VIDEO_S3_SCHEME": "http",
    "VIDEO_S3_REGION": "us-east-1",
}


def test_chunk_external_cli_creates_and_refreshes_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from geneva_examples.examples.video import chunk_external_video as mod

    for key, value in _VIDEO_ENV.items():
        monkeypatch.setenv(key, value)
    table = FakeTable(names=["video_id", "video_uri"])
    conn = FakeConn(table=table, is_remote=False)
    monkeypatch.setattr(mod, "connect", lambda _cfg: conn)
    monkeypatch.setattr(mod, "runtime_session", lambda *_a, **_k: nullcontext())

    result = CliRunner().invoke(cli.chunk_videos_external, ["--mode", "local"])

    assert result.exit_code == 0, result.output
    assert "video_clips" in conn.dropped
    assert "video_clips" in conn.created


def test_chunk_external_cli_requires_video_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from geneva_examples.examples.video import chunk_external_video as mod

    for key in (*_VIDEO_ENV, "VIDEO_S3_BUCKET"):
        monkeypatch.delenv(key, raising=False)
    conn = FakeConn(table=FakeTable(), is_remote=False)
    monkeypatch.setattr(mod, "connect", lambda _cfg: conn)

    result = CliRunner().invoke(cli.chunk_videos_external, ["--mode", "local"])

    assert result.exit_code != 0
    assert "missing video-bucket credentials" in str(result.exception)
    assert "video_clips" not in conn.created  # failed before touching the table
