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


class _RowsTable(FakeTable):
    """FakeTable whose read chain returns canned clips rows."""

    def __init__(self, rows: list[dict]) -> None:
        super().__init__(names=["video_id", "chunk_id", "errors"])
        self._data = rows

    def to_list(self) -> list[dict]:
        return self._data


def _clips_rows(expected: dict[str, set[str]]) -> list[dict]:
    """Clips rows that satisfy the demo's expected error classes exactly."""
    rows = []
    for vid, tags in expected.items():
        for i, tag in enumerate(sorted(tags)):
            clean = tag == "clean"
            rows.append(
                {
                    "video_id": vid,
                    "chunk_id": i if clean else None,
                    "start_sec": 0.0 if clean else None,
                    "end_sec": 1.0 if clean else None,
                    "clip_bytes": b"clip" if clean else None,
                    "frame": b"jpg" if clean else None,
                    "errors": None if clean else [f"{tag}: injected detail"],
                }
            )
    return rows


def test_chunk_faults_cli_seeds_chunks_and_reports(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from geneva_examples.examples.video import chunk_faults, chunk_openvid

    calls: dict = {}
    monkeypatch.setattr(
        chunk_openvid, "run", lambda _cfg, **kwargs: calls.update(kwargs)
    )
    clips = _RowsTable(_clips_rows(chunk_faults.EXPECTED))
    conn = FakeConn(tables={"video_clips_faults": clips}, is_remote=False)
    monkeypatch.setattr(chunk_faults, "connect", lambda _cfg: conn)

    result = CliRunner().invoke(
        cli.chunk_videos_faults, ["--mode", "local", "--data-dir", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    # The poisoned source dataset was really written under --data-dir...
    assert (tmp_path / "train.lance").exists()
    # ...the pointer table was created, and the pipeline got the demo wiring.
    assert "videos_faults" in conn.created
    assert calls["source_table"] == "videos_faults"
    assert calls["clips_table"] == "video_clips_faults"
    assert calls["openvid_uri"] == str(tmp_path.resolve())
    assert calls["read_retries"] == 2


def test_chunk_faults_cli_fails_when_a_clean_video_grows_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from geneva_examples.examples.video import chunk_faults, chunk_openvid

    monkeypatch.setattr(chunk_openvid, "run", lambda _cfg, **_kw: None)
    rows = _clips_rows(chunk_faults.EXPECTED)
    for row in rows:
        if row["video_id"] == "good":
            row["errors"] = ["decode_failed: should not happen"]
    conn = FakeConn(tables={"video_clips_faults": _RowsTable(rows)}, is_remote=False)
    monkeypatch.setattr(chunk_faults, "connect", lambda _cfg: conn)

    result = CliRunner().invoke(
        cli.chunk_videos_faults, ["--mode", "local", "--data-dir", str(tmp_path)]
    )

    assert result.exit_code == 1


def test_chunk_faults_refuses_enterprise_mode_with_local_data_dir(tmp_path) -> None:
    from geneva_examples.core.config import Config
    from geneva_examples.examples.video import chunk_faults

    cfg = Config(mode="enterprise")
    with pytest.raises(SystemExit):
        chunk_faults.run(cfg, data_dir=str(tmp_path))
