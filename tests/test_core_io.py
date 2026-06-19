"""Tests for the HF image / video I/O utilities (pure parts, mocked network)."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
from PIL import Image

from geneva_examples.core.utils import images, videos


# --------------------------------------------------------------------------- #
# images.py
# --------------------------------------------------------------------------- #
class _Query:
    def __init__(self, rows):
        self._rows = rows

    def select(self, _cols):
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def to_list(self):
        return self._rows


class _Table:
    def __init__(self, rows):
        self._rows = rows

    def search(self, _expr=None):
        return _Query(list(self._rows))


def test_decode_images_from_table(make_png_bytes):
    rows = [
        {"image_id": 0, "image": make_png_bytes((10, 20))},
        {"image_id": 1, "image": make_png_bytes((30, 40))},
    ]
    imgs = images.decode_images_from_table(_Table(rows))
    assert [im.size for im in imgs] == [(10, 20), (30, 40)]


def test_decode_images_respects_limit(make_png_bytes):
    rows = [{"image_id": i, "image": make_png_bytes((8, 8))} for i in range(5)]
    assert len(images.decode_images_from_table(_Table(rows), limit=2)) == 2


def test_load_hf_image_batches_batches_and_maps_fields(monkeypatch):
    fake_rows = [
        {
            "image": Image.new("RGB", (8, 8)),
            "label": i,
            "image_id": i,
            "label_cat_dog": "cat",
        }
        for i in range(3)
    ]

    def fake_load_dataset(name, split):
        return fake_rows

    monkeypatch.setattr("datasets.load_dataset", fake_load_dataset)
    batches = images.load_hf_image_batches("ds", "train", num_images=3, frag_size=2)
    assert [b.num_rows for b in batches] == [2, 1]  # 3 rows, frag_size 2
    first = batches[0].to_pylist()[0]
    assert set(first) == {"image", "label", "image_id", "label_cat_dog"}
    assert (
        isinstance(first["image"], bytes) and first["image"][:8] == b"\x89PNG\r\n\x1a\n"
    )


# --------------------------------------------------------------------------- #
# videos.py — download helpers
# --------------------------------------------------------------------------- #
def test_download_cache_hit(tmp_path: Path):
    dest = tmp_path / "v.mp4"
    dest.write_bytes(b"cached-bytes")
    assert videos._download("http://example/v.mp4", dest) == b"cached-bytes"


def test_download_fetches_when_absent(tmp_path: Path, monkeypatch):
    class _Resp:
        def __init__(self, data):
            self._data, self._done = data, False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, _n):
            if self._done:
                return b""
            self._done = True
            return self._data

    monkeypatch.setattr(videos.urllib.request, "urlopen", lambda _req: _Resp(b"DATA"))
    dest = tmp_path / "nested" / "v.mp4"
    assert videos._download("http://example/v.mp4", dest) == b"DATA"
    assert dest.read_bytes() == b"DATA"


def test_download_video_batches_fragments(monkeypatch):
    monkeypatch.setattr(
        videos, "_download", lambda _url, dest: f"b-{dest.name}".encode()
    )
    one = videos.download_video_batches(
        [("a", "http://x/a.mp4"), ("b", "http://x/b.webm")], "cache", frag_size=1
    )
    assert [b.num_rows for b in one] == [1, 1]
    assert one[0].schema.names == ["video_id", "video"]

    two = videos.download_video_batches(
        [("a", "u"), ("b", "u"), ("c", "u")], "cache", frag_size=2
    )
    assert [b.num_rows for b in two] == [2, 1]


def test_to_batch_schema():
    batch = videos._to_batch([{"video_id": "v", "video": b"x"}])
    assert batch.schema.field("video").type == pa.large_binary()


# --------------------------------------------------------------------------- #
# videos.py — OpenVid reference normalization
# --------------------------------------------------------------------------- #
def _openvid_batch(rowids, blobs, embedding_type=pa.list_(pa.float32(), 1024)):
    n = len(rowids)
    dim = 1024
    cols = {
        "video_path": pa.array([f"clip_{i}.mp4" for i in range(n)], pa.string()),
        "video_blob": pa.array(blobs, pa.large_binary()),
        "caption": pa.array(["a caption"] * n, pa.string()),
        "embedding": pa.array([[float(i)] * dim for i in range(n)], embedding_type),
        "aesthetic_score": pa.array([1.0] * n, pa.float64()),
        "motion_score": pa.array([2.0] * n, pa.float64()),
        "temporal_consistency_score": pa.array([3.0] * n, pa.float64()),
        "camera_motion": pa.array(["pan"] * n, pa.string()),
        "fps": pa.array([30.0] * n, pa.float64()),
        "seconds": pa.array([5.0] * n, pa.float64()),
        "frame": pa.array([150] * n, pa.int64()),
        "_rowid": pa.array(rowids, pa.int64()),
    }
    return pa.RecordBatch.from_arrays(list(cols.values()), names=list(cols))


def test_normalize_openvid_maps_fields():
    batch = _openvid_batch([10, 11], [b"blob0", b"blob1"])
    out = videos.normalize_openvid_reference_batch(batch)
    assert out.schema.equals(videos._openvid_target_schema())
    rows = out.to_pylist()
    assert rows[0]["video_id"] == "clip_0.mp4"
    assert rows[0]["openvid_rowid"] == 10
    assert rows[0]["camera_motion"] == "pan"
    assert len(rows[0]["embedding"]) == 1024


def test_normalize_openvid_filters_null_video():
    batch = _openvid_batch([10, 11], [b"blob0", None])  # second row has no blob
    out = videos.normalize_openvid_reference_batch(batch, skip_null_video=True)
    assert out.num_rows == 1
    assert out.to_pylist()[0]["openvid_rowid"] == 10


def test_normalize_openvid_all_null_returns_empty_typed_batch():
    batch = _openvid_batch([10], [None])
    out = videos.normalize_openvid_reference_batch(batch, skip_null_video=True)
    assert out.num_rows == 0
    assert out.schema.equals(videos._openvid_target_schema())


def test_normalize_openvid_casts_embedding_dtype():
    # Source surfaces embedding as variable-length list<float64>; must cast.
    batch = _openvid_batch([10], [b"blob"], embedding_type=pa.list_(pa.float64()))
    out = videos.normalize_openvid_reference_batch(batch)
    assert out.schema.field("embedding").type == pa.list_(pa.float32(), 1024)


def test_openvid_source_columns_constant():
    assert videos.OPENVID_SOURCE_COLUMNS[0] == "video_path"
    assert "video_blob" in videos.OPENVID_SOURCE_COLUMNS
