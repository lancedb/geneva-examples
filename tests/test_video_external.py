"""Tests for the external-storage video pipeline: cred helpers + spec wiring.

The ``run()`` bodies of ``ingest-videos-external`` / ``chunk-videos-external``
are covered by the CLI smoke tests; here we unit-test their pure helpers
(endpoint peeling, ``VIDEO_S3_*`` credential resolution) and pin the spec
surface the two new steps and the ``frame-embed --reset`` flag expose.
"""

from __future__ import annotations

import pytest

from geneva_examples.examples import video
from geneva_examples.examples.video.ingest_external_refs import (
    _endpoint_and_scheme,
    _resolve_video_creds,
)

_ENV_KEYS = (
    "VIDEO_S3_BUCKET",
    "VIDEO_S3_ENDPOINT",
    "VIDEO_S3_ACCESS_KEY",
    "VIDEO_S3_SECRET_KEY",
    "VIDEO_S3_REGION",
)


@pytest.fixture
def scrubbed_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Remove any ambient VIDEO_S3_* vars so fallback behavior is deterministic."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("https://minio.example.com:9000/", ("minio.example.com:9000", "https")),
        ("http://minio.example.com:9000", ("minio.example.com:9000", "http")),
        ("minio.example.com:9000", ("minio.example.com:9000", "https")),
        ("minio.example.com/", ("minio.example.com", "https")),
    ],
)
def test_endpoint_and_scheme_peels_url(endpoint, expected):
    # pyarrow's S3FileSystem wants endpoint_override as a bare host; the scheme
    # travels separately (https when the endpoint doesn't say).
    assert _endpoint_and_scheme(endpoint) == expected


def test_resolve_video_creds_prefers_explicit_args(scrubbed_env):
    scrubbed_env.setenv("VIDEO_S3_BUCKET", "env-bucket")
    scrubbed_env.setenv("VIDEO_S3_REGION", "env-region")
    resolved = _resolve_video_creds(
        "flag-bucket", "minio.test:9000", "ak", "sk", "flag-region"
    )
    assert resolved == ("flag-bucket", "minio.test:9000", "ak", "sk", "flag-region")


def test_resolve_video_creds_falls_back_to_env(scrubbed_env):
    scrubbed_env.setenv("VIDEO_S3_BUCKET", "env-bucket")
    scrubbed_env.setenv("VIDEO_S3_ENDPOINT", "https://minio.test:9000")
    scrubbed_env.setenv("VIDEO_S3_ACCESS_KEY", "env-ak")
    scrubbed_env.setenv("VIDEO_S3_SECRET_KEY", "env-sk")
    scrubbed_env.setenv("VIDEO_S3_REGION", "eu-west-1")
    resolved = _resolve_video_creds("", "", "", "", "")
    assert resolved == (
        "env-bucket",
        "https://minio.test:9000",
        "env-ak",
        "env-sk",
        "eu-west-1",
    )


def test_resolve_video_creds_defaults_region(scrubbed_env):
    _ = scrubbed_env
    resolved = _resolve_video_creds("b", "e", "ak", "sk", "")
    assert resolved[-1] == "us-east-1"


def test_resolve_video_creds_reports_every_missing_field(scrubbed_env):
    _ = scrubbed_env
    with pytest.raises(RuntimeError) as excinfo:
        _resolve_video_creds("b", "", "", "", "")
    message = str(excinfo.value)
    assert "missing video-bucket credentials" in message
    assert "video_bucket" not in message  # provided, so not reported
    for name in ("video_endpoint", "video_access_key", "video_secret_key"):
        assert name in message


def test_external_steps_registered_with_expected_params():
    ingest = video.EXAMPLE.step("ingest-videos-external")
    assert ingest.run is video.ingest_external_refs.run
    params = {p.name: p for p in ingest.params}
    assert params["suffix"].default == ".mp4"
    assert params["limit"].default == 100
    assert params["sample"].type is str and params["sample"].default == ""

    chunk = video.EXAMPLE.step("chunk-videos-external")
    assert chunk.run is video.chunk_external_video.run
    params = {p.name: p for p in chunk.params}
    assert params["source_task_size"].default == 1  # fan out one video per task
    assert params["detach"].type is bool and params["detach"].default is False
    assert params["uri_column"].default == "video_uri"


def test_frame_embed_reset_defaults_to_incremental():
    # The destructive drop+recompute is opt-in (--reset); the default embeds
    # only rows whose embedding is still null.
    params = {p.name: p for p in video.EXAMPLE.step("frame-embed").params}
    reset = params["reset"]
    assert reset.type is bool
    assert reset.default is False
