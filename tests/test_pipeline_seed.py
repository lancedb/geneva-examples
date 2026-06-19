"""Tests for seed-video-clips' decode + constant-bytes UDF (driver-runnable)."""

from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from geneva_examples.pipeline import seed_video_clips


def test_build_constant_bytes_udf_returns_payload():
    udf = seed_video_clips.build_constant_bytes_udf(
        input_column="x",
        payload=b"PAYLOAD",
        manifest=None,
        num_cpus=1.0,
        memory_bytes=1 << 20,
        checkpoint_size=8,
        task_size=8,
    )
    # Ignores its input and returns the captured bytes for every row.
    assert udf(b"anything") == b"PAYLOAD"
    assert udf.func("ignored") == b"PAYLOAD"


def test_decode_seed_clip(mp4_bytes):
    frame, clip, start, end = seed_video_clips._decode_seed_clip(mp4_bytes, 1.0)
    assert start == 0.0
    assert end == 1.0
    assert isinstance(clip, bytes) and clip  # a remuxed mp4
    # frame is a JPEG of the first frame
    with Image.open(BytesIO(frame)) as img:
        assert img.format == "JPEG"
        assert max(img.size) <= 512


def test_decode_seed_clip_rejects_garbage():
    with pytest.raises(Exception):  # noqa: B017 (av raises its own error type)
        seed_video_clips._decode_seed_clip(b"not a video", 1.0)
