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


def make_pdf(text: str = "Hello chunker world") -> bytes:
    """Build a tiny single-page PDF whose one text run is ``text``.

    Hand-assembled with a correct xref table (computed byte offsets) so pypdf
    parses it and ``extract_text()`` returns ``text`` — enough to exercise the
    reused ``extract_pages`` / ``chunk_pages`` UDFs without reportlab.
    """
    stream = b"BT /F1 24 Tf 40 150 Td (" + text.encode("latin-1") + b") Tj ET"
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 300]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length "
        + str(len(stream)).encode()
        + b">>stream\n"
        + stream
        + b"\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += str(i).encode() + b" 0 obj" + body + b"endobj\n"
    xref_pos = len(out)
    out += b"xref\n0 " + str(len(objs) + 1).encode() + b"\n0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += b"trailer<</Size " + str(len(objs) + 1).encode() + b"/Root 1 0 R>>\n"
    out += b"startxref\n" + str(xref_pos).encode() + b"\n%%EOF"
    return bytes(out)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A studio_data-shaped dir: images/, videos/, audio/, pdfs/, input.csv."""
    root = tmp_path / "data"
    for sub in ("images", "videos", "audio", "pdfs"):
        (root / sub).mkdir(parents=True)

    for i, color in enumerate(IMAGE_COLORS):
        (root / "images" / f"img_{i}.png").write_bytes(
            make_png(color, (64 + 8 * i, 48 + 8 * i))
        )
    # A non-image file that extension filtering must skip.
    (root / "images" / "notes.txt").write_text("not an image")

    for i in range(2):
        (root / "pdfs" / f"doc_{i}.pdf").write_bytes(make_pdf(f"page text {i}"))
    # A non-pdf file that extension filtering must skip.
    (root / "pdfs" / "notes.txt").write_text("not a pdf")

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
def pdf_bytes() -> bytes:
    """A tiny single-page text PDF for the PDF extract/chunk UDFs."""
    return make_pdf("the quick brown fox jumps over the lazy dog")


@pytest.fixture
def make_png_bytes():
    """Factory returning PNG bytes of a given ``(width, height)`` size."""

    def _make(
        size: tuple[int, int], color: tuple[int, int, int] = (120, 200, 80)
    ) -> bytes:
        return make_png(color, size)

    return _make
