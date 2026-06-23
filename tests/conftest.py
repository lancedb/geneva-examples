"""Shared fixtures: a UDF Studio data directory with synthetic media + CSV."""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest
from _fakes import install_fake_geneva
from PIL import Image


@pytest.fixture
def fake_geneva(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock the Geneva cluster boundary for CLI smoke tests (see tests/_fakes.py)."""
    install_fake_geneva(monkeypatch)


IMAGE_COLORS = [(220, 40, 40), (40, 180, 90), (60, 90, 220)]


def make_png(color: tuple[int, int, int], size: tuple[int, int]) -> bytes:
    """Encode a solid-color PNG to bytes."""
    buf = io.BytesIO()
    Image.new("RGB", color=color, size=size).save(buf, format="PNG")
    return buf.getvalue()


def make_mp4(seconds: float = 3.0, fps: int = 10) -> bytes:
    """Encode a tiny in-memory H.264 mp4 for exercising the video chunker."""
    import av
    import numpy as np

    buf = io.BytesIO()
    container = av.open(buf, mode="w", format="mp4")
    stream = container.add_stream("libx264", rate=fps)
    stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
    for i in range(int(seconds * fps)):
        arr = np.full((48, 64, 3), (i * 8) % 255, dtype=np.uint8)
        for packet in stream.encode(av.VideoFrame.from_ndarray(arr, format="rgb24")):
            container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()
    return buf.getvalue()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A studio_data-shaped directory: images/, videos/, audio/, input.csv."""
    root = tmp_path / "data"
    for sub in ("images", "videos", "audio"):
        (root / sub).mkdir(parents=True)

    for i, color in enumerate(IMAGE_COLORS):
        (root / "images" / f"img_{i}.png").write_bytes(
            make_png(color, (64 + 8 * i, 48 + 8 * i))
        )
    # A non-image file that extension filtering must skip.
    (root / "images" / "notes.txt").write_text("not an image")

    with (root / "input.csv").open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "text"])
        writer.writerow(["1", "hello world"])
        writer.writerow(["2", "the quick brown fox"])
    return root


@pytest.fixture
def library_path(tmp_path: Path) -> Path:
    """A fresh local LanceDB library location (created on first save)."""
    return tmp_path / "udf_library"


@pytest.fixture
def mp4_bytes() -> bytes:
    """A tiny 3-second in-memory mp4 for the video chunker."""
    return make_mp4(seconds=3.0, fps=10)


@pytest.fixture
def make_png_bytes():
    """Factory returning PNG bytes of a given ``(width, height)`` size."""

    def _make(
        size: tuple[int, int], color: tuple[int, int, int] = (120, 200, 80)
    ) -> bytes:
        return make_png(color, size)

    return _make
