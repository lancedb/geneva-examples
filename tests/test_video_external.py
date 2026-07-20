"""Tests for the external-storage video pipeline: cred helpers + spec wiring.

The ``run()`` bodies of ``ingest-videos-external`` / ``chunk-videos-external``
are covered by the CLI smoke tests; here we unit-test their pure helpers
(endpoint peeling, video credential resolution from flags + ``config.yaml``
``s3_*`` settings) and pin the spec surface the two new steps and the
``frame-embed --reset`` flag expose.
"""

from __future__ import annotations

import pytest

from geneva_examples.core.config import Config
from geneva_examples.examples import video
from geneva_examples.examples.video.ingest_external_refs import (
    _endpoint_and_scheme,
    _resolve_video_creds,
    _video_id,
)

_S3_CFG = dict(
    s3_access_key="cfg-ak",
    s3_secret_key="cfg-sk",  # noqa: S106 (fake test cred)
    s3_endpoint="http://cfg-minio.test:9000",
    s3_region="eu-central-1",
)


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


def test_endpoint_and_scheme_honors_default_for_bare_host():
    # A bare host takes the caller's default (http when aws_allow_http is set);
    # an explicit URL scheme still wins over it.
    assert _endpoint_and_scheme("minio.test:9000", default_scheme="http") == (
        "minio.test:9000",
        "http",
    )
    assert _endpoint_and_scheme("https://minio.test:9000", default_scheme="http") == (
        "minio.test:9000",
        "https",
    )


def test_resolve_video_creds_prefers_explicit_flags():
    resolved = _resolve_video_creds(
        Config(**_S3_CFG),
        bucket="flag-bucket",
        endpoint="minio.test:9000",
        access_key="ak",
        secret_key="sk",  # noqa: S106 (fake test cred)
        region="flag-region",
    )
    assert resolved == ("flag-bucket", "minio.test:9000", "ak", "sk", "flag-region")


def test_resolve_video_creds_falls_back_to_config_storage():
    # With no flags, the same s3_* block that backs the LanceDB connection is
    # used — the corpus usually lives in the same object store.
    resolved = _resolve_video_creds(
        Config(**_S3_CFG),
        bucket="vids",
        endpoint="",
        access_key="",
        secret_key="",
        region="",
    )
    assert resolved == (
        "vids",
        "http://cfg-minio.test:9000",
        "cfg-ak",
        "cfg-sk",
        "eu-central-1",
    )


def test_resolve_video_creds_defaults_region():
    cfg = Config(**{**_S3_CFG, "s3_region": None})
    resolved = _resolve_video_creds(
        cfg, bucket="vids", endpoint="", access_key="", secret_key="", region=""
    )
    assert resolved[-1] == "us-east-1"


def test_resolve_video_creds_ignores_ambient_env(monkeypatch: pytest.MonkeyPatch):
    # VIDEO_S3_* is the worker-env transport the chunk CLI *writes*, not a
    # driver-side input: ambient values must not satisfy the resolution.
    for key in ("BUCKET", "ENDPOINT", "ACCESS_KEY", "SECRET_KEY", "REGION"):
        monkeypatch.setenv(f"VIDEO_S3_{key}", "ambient")
    with pytest.raises(RuntimeError, match="missing video-bucket credentials"):
        _resolve_video_creds(
            Config(),
            bucket="vids",
            endpoint="",
            access_key="",
            secret_key="",
            region="",
        )


def test_resolve_video_creds_reports_every_missing_field():
    with pytest.raises(RuntimeError) as excinfo:
        _resolve_video_creds(
            Config(), bucket="b", endpoint="", access_key="", secret_key="", region=""
        )
    message = str(excinfo.value)
    assert "missing video-bucket credentials" in message
    assert "config.yaml" in message
    assert "video_bucket" not in message  # provided, so not reported
    for name in ("video_endpoint", "video_access_key", "video_secret_key"):
        assert name in message


def test_resolve_video_creds_bucket_optional_for_chunk():
    # The chunk CLI's video_uri rows already carry the bucket.
    resolved = _resolve_video_creds(
        Config(**_S3_CFG),
        endpoint="",
        access_key="",
        secret_key="",
        region="",
        require_bucket=False,
    )
    assert resolved[0] == ""
    assert resolved[2] == "cfg-ak"


@pytest.mark.parametrize(
    ("path", "root", "suffix", "expected"),
    [
        ("vids/clip.mp4", "vids", ".mp4", "clip"),  # flat: just the basename
        ("vids/raw/a/clip.mp4", "vids/raw", ".mp4", "a/clip"),  # nested: unique
        ("vids/CLIP.MP4", "vids", ".mp4", "CLIP"),  # strip matches the filter
        ("vids/clip.mp4", "vids", "", "clip.mp4"),  # no suffix -> untouched
    ],
)
def test_video_id_is_root_relative_and_case_insensitive(path, root, suffix, expected):
    assert _video_id(path, root, suffix) == expected


def test_external_steps_registered_with_expected_params():
    ingest = video.EXAMPLE.step("ingest-videos-external")
    assert ingest.run is video.ingest_external_refs.run
    params = {p.name: p for p in ingest.params}
    assert params["suffix"].default == ".mp4"
    assert params["limit"].default == 100
    assert params["limit"].min == 0  # negative --limit is a CLI usage error
    assert params["sample"].type is str and params["sample"].default == ""
    # Cred params default empty = "resolve from config.yaml s3_*".
    assert params["video_endpoint"].default == ""
    assert params["video_region"].default == ""

    chunk = video.EXAMPLE.step("chunk-videos-external")
    assert chunk.run is video.chunk_external_video.run
    params = {p.name: p for p in chunk.params}
    assert params["source_task_size"].default == 1  # fan out one video per task
    assert params["detach"].type is bool and params["detach"].default is False
    assert params["uri_column"].default == "video_uri"
    assert params["video_region"].default == ""


def test_frame_embed_reset_defaults_to_incremental():
    # The destructive drop+recompute is opt-in (--reset); the default embeds
    # only rows whose embedding is still null.
    params = {p.name: p for p in video.EXAMPLE.step("frame-embed").params}
    reset = params["reset"]
    assert reset.type is bool
    assert reset.default is False
