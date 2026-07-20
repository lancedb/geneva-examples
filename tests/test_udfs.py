"""Tests for the reusable Geneva callables in the example packages.

The geneva-decorated UDFs/chunkers are callable on the driver, so the
lightweight ones (imageinfo, the video chunker) run for real here. The heavy
model UDFs (clip/blip/openpose) need torch + weights, so we only assert their
runtime-pip manifests are well-formed and env-overridable.
"""

from __future__ import annotations

import importlib
import inspect
import io
import types

import pytest

from geneva_examples.examples._shared import blip, clip
from geneva_examples.examples.audio import transcribe as audio_transcribe
from geneva_examples.examples.audio import tts as audio_tts
from geneva_examples.examples.images import imageinfo
from geneva_examples.examples.pdf import document as pdf_udfs
from geneva_examples.examples.video import chunkers, chunkers_uri, openpose


def test_file_size_udf_runs():
    udf = imageinfo.build_file_size_udf(input_column="image", manifest=None)
    assert udf(b"abcd") == 4
    assert udf.func(b"abcde") == 5  # underlying function too


def test_dimensions_udf_runs(make_png_bytes):
    udf = imageinfo.build_dimensions_udf(input_column="image", manifest=None)
    out = udf(make_png_bytes((81, 37)))
    assert out == {"width": 81, "height": 37}


@pytest.mark.parametrize(
    ("runtime_pip", "expected_substr"),
    [
        (imageinfo.IMAGEINFO_RUNTIME_PIP, "pillow"),
        (clip.CLIP_RUNTIME_PIP, "open-clip-torch"),
        (blip.BLIP_RUNTIME_PIP, "transformers"),
        (openpose.OPENPOSE_RUNTIME_PIP, "controlnet"),
        (chunkers.VIDEO_RUNTIME_PIP, "av"),
        (pdf_udfs.PDF_RUNTIME_PIP, "pypdf"),
        (audio_tts.MMS_TTS_RUNTIME_PIP, "transformers"),
        (audio_transcribe.WHISPER_RUNTIME_PIP, "transformers"),
    ],
)
def test_runtime_pip_lists_are_well_formed(runtime_pip, expected_substr):
    assert isinstance(runtime_pip, list)
    assert runtime_pip
    assert all(isinstance(spec, str) and spec for spec in runtime_pip)
    assert any(spec.startswith("geneva==") for spec in runtime_pip)
    assert any(expected_substr in spec for spec in runtime_pip)


def test_runtime_pip_env_override(monkeypatch):
    monkeypatch.setenv("GENEVA_PACKAGE_SPEC", "geneva==9.9.9")
    importlib.reload(clip)
    try:
        assert "geneva==9.9.9" in clip.CLIP_RUNTIME_PIP
    finally:
        monkeypatch.delenv("GENEVA_PACKAGE_SPEC", raising=False)
        importlib.reload(clip)  # restore module defaults for other tests


def test_chunk_video_udtf_runs_on_real_mp4(mp4_bytes):
    udtf = chunkers.chunk_video_udtf(chunk_seconds=1.0, manifest=None)
    rows = list(udtf.func(mp4_bytes))
    assert len(rows) == 3  # ~3s video / 1s windows
    first = rows[0]
    assert set(first) == {"chunk_id", "start_sec", "end_sec", "clip_bytes", "frame"}
    assert first["chunk_id"] == 0
    assert first["start_sec"] == 0.0
    assert isinstance(first["frame"], bytes) and first["frame"]
    # windows are contiguous and ordered
    assert [r["chunk_id"] for r in rows] == [0, 1, 2]


def test_chunk_video_udtf_respects_num_clips(mp4_bytes):
    udtf = chunkers.chunk_video_udtf(chunk_seconds=1.0, manifest=None, num_clips=2)
    assert len(list(udtf.func(mp4_bytes))) == 2


def test_chunk_video_udtf_handles_none():
    udtf = chunkers.chunk_video_udtf(chunk_seconds=1.0, manifest=None)
    assert list(udtf.func(None)) == []


def test_chunk_video_udtf_respects_max_video_s(mp4_bytes):
    # The synth clip is ~3s; a 1s ceiling skips it entirely.
    udtf = chunkers.chunk_video_udtf(chunk_seconds=1.0, manifest=None, max_video_s=1.0)
    assert list(udtf.func(mp4_bytes)) == []


def test_pdf_factories_reuse_geneva_udfs_with_manifest():
    # The factories wrap geneva.udfs.document UDFs, attaching this repo's
    # manifest while preserving the parameter-name-inferred input columns.
    from geneva.manifest import GenevaManifest

    manifest = (
        GenevaManifest.create_pip("pdf-test").pip(pdf_udfs.PDF_RUNTIME_PIP).build()
    )
    extract = pdf_udfs.build_extract_pages_udf(manifest=manifest)
    chunk = pdf_udfs.build_chunk_pages_udf(manifest=manifest)
    assert extract.input_columns == ["pdf_bytes"]
    assert chunk.input_columns == ["pages"]
    assert extract.manifest is manifest
    assert chunk.manifest is manifest
    # Fresh versions each build so re-runs re-materialize the columns.
    assert (
        extract.version != pdf_udfs.build_extract_pages_udf(manifest=manifest).version
    )


def test_pdf_extract_then_chunk_runs_on_real_pdf(pdf_bytes):
    # extract_pages -> chunk_pages chains on the driver (pypdf + langchain).
    pages = pdf_udfs.build_extract_pages_udf(manifest=None).func(pdf_bytes)
    assert pages == [
        {"page_number": 0, "text": "the quick brown fox jumps over the lazy dog"}
    ]
    chunks = pdf_udfs.build_chunk_pages_udf(manifest=None).func(pages)
    assert len(chunks) == 1
    assert set(chunks[0]) == {"page_number", "chunk_id", "chunk"}
    assert chunks[0]["chunk"] == "the quick brown fox jumps over the lazy dog"


def test_pdf_extract_handles_empty():
    assert pdf_udfs.build_extract_pages_udf(manifest=None).func(None) is None
    assert pdf_udfs.build_chunk_pages_udf(manifest=None).func(None) is None


def test_chunk_blob_video_udtf_reads_from_lance(tmp_path, mp4_bytes):
    import lance
    import pyarrow as pa

    # A Lance dataset with a blob-encoded column, as OpenVid stores videos.
    blob_field = pa.field(
        "video_blob", pa.large_binary(), metadata={"lance-encoding:blob": "true"}
    )
    schema = pa.schema([blob_field, pa.field("video_id", pa.string())])
    table = pa.table({"video_blob": [mp4_bytes], "video_id": ["v0"]}, schema=schema)
    uri = str(tmp_path / "src.lance")
    lance.write_dataset(table, uri)

    udtf = chunkers.chunk_blob_video_udtf(
        source_uri=uri,
        blob_column="video_blob",
        pointer_column="openvid_rowid",
        chunk_seconds=1.0,
        manifest=None,
    )
    rows = list(udtf.func(0))  # row 0's blob
    assert len(rows) == 3
    assert set(rows[0]) == {"chunk_id", "start_sec", "end_sec", "clip_bytes", "frame"}
    assert list(udtf.func(None)) == []  # null pointer -> nothing


def _install_fake_video_s3(
    monkeypatch,
    files: dict[str, bytes],
    *,
    sizes: dict[str, int] | None = None,
) -> tuple[list[dict], list[str]]:
    """Back the URI chunker with an in-memory object store + ASSETS_S3_* creds.

    The chunker closure constructs ``pyarrow.fs.S3FileSystem`` at call time from
    worker env vars, so patching the class and setting the env is the whole
    cluster boundary. Returns (constructor kwargs, opened paths) for asserts.
    """
    constructed: list[dict] = []
    opened: list[str] = []

    class _FS:
        def open_input_file(self, path):
            opened.append(path)
            return io.BytesIO(files[path])

        def get_file_info(self, path):
            size = (sizes or {}).get(path, len(files.get(path, b"")))
            return types.SimpleNamespace(size=size)

    def _factory(**kwargs):
        constructed.append(kwargs)
        return _FS()

    monkeypatch.setattr("pyarrow.fs.S3FileSystem", _factory)
    for key, value in {
        "ASSETS_S3_ACCESS_KEY": "ak",
        "ASSETS_S3_SECRET_KEY": "sk",
        "ASSETS_S3_ENDPOINT": "minio.test:9000",
        "ASSETS_S3_SCHEME": "http",
        "ASSETS_S3_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(key, value)
    return constructed, opened


def test_chunk_uri_video_udtf_streams_from_object_store(monkeypatch, mp4_bytes):
    constructed, opened = _install_fake_video_s3(
        monkeypatch, {"vids/v0.mp4": mp4_bytes}
    )
    udtf = chunkers_uri.chunk_uri_video_udtf(chunk_seconds=1.0, manifest=None)
    rows = list(udtf.func("s3://vids/v0.mp4"))
    assert len(rows) == 3  # ~3s video / 1s windows
    assert set(rows[0]) == {"chunk_id", "start_sec", "end_sec", "clip_bytes", "frame"}
    assert [r["chunk_id"] for r in rows] == [0, 1, 2]
    # The s3:// prefix is stripped to a bucket/key path for pyarrow.
    assert opened and all(p == "vids/v0.mp4" for p in opened)
    # The filesystem is built once and cached across rows (per-actor cache).
    list(udtf.func("s3://vids/v0.mp4"))
    assert len(constructed) == 1


def test_chunk_uri_video_udtf_clips_are_decodable(monkeypatch, mp4_bytes):
    # Regression test for the keyframe fix: grabbing the start-frame JPEG
    # advances the demuxer, so without the unconditional re-seek before the
    # remux the start=0 window emits a clip with no leading keyframe — it
    # remuxes "successfully" but decodes to zero frames.
    import av

    _install_fake_video_s3(monkeypatch, {"vids/v0.mp4": mp4_bytes})
    udtf = chunkers_uri.chunk_uri_video_udtf(chunk_seconds=1.0, manifest=None)
    rows = list(udtf.func("s3://vids/v0.mp4"))
    assert rows
    for row in rows:
        with av.open(io.BytesIO(row["clip_bytes"])) as clip:
            frames = sum(1 for _ in clip.decode(clip.streams.video[0]))
        assert frames > 0, f"chunk {row['chunk_id']} is undecodable"
        assert row["frame"]  # start-frame JPEG captured alongside


def test_chunk_uri_video_udtf_skips_rows_without_credentials(monkeypatch, mp4_bytes):
    _install_fake_video_s3(monkeypatch, {"vids/v0.mp4": mp4_bytes})
    monkeypatch.delenv("ASSETS_S3_ACCESS_KEY")
    udtf = chunkers_uri.chunk_uri_video_udtf(chunk_seconds=1.0, manifest=None)
    # Missing worker creds are a config error surfaced as warn+skip, not a raise.
    assert list(udtf.func("s3://vids/v0.mp4")) == []


def test_chunk_uri_video_udtf_respects_max_video_mb(monkeypatch, mp4_bytes):
    # The cap is decimal MB, matching the videos table's size_mb column: a
    # 2.05 MB object is over a 2.0 cap (binary MiB would let it through).
    _install_fake_video_s3(
        monkeypatch,
        {"vids/v0.mp4": mp4_bytes},
        sizes={"vids/v0.mp4": 2_050_000},
    )
    udtf = chunkers_uri.chunk_uri_video_udtf(
        chunk_seconds=1.0, manifest=None, max_video_mb=2.0
    )
    assert list(udtf.func("s3://vids/v0.mp4")) == []  # stat says too big; skipped
    udtf = chunkers_uri.chunk_uri_video_udtf(
        chunk_seconds=1.0, manifest=None, max_video_mb=2.1
    )
    assert list(udtf.func("s3://vids/v0.mp4"))  # under the cap; processed


def test_chunk_uri_video_udtf_declares_uri_column(monkeypatch, mp4_bytes):
    # input_columns must track uri_column so the CLI's source projection and
    # the chunker agree (geneva validates them against each other server-side);
    # the UDF arg is positional, so a renamed column still feeds it.
    _install_fake_video_s3(monkeypatch, {"vids/v0.mp4": mp4_bytes})
    udtf = chunkers_uri.chunk_uri_video_udtf(chunk_seconds=1.0, manifest=None)
    assert udtf.input_columns == ["video_uri"]
    udtf = chunkers_uri.chunk_uri_video_udtf(
        uri_column="my_uri", chunk_seconds=1.0, manifest=None
    )
    assert udtf.input_columns == ["my_uri"]
    assert len(list(udtf.func("s3://vids/v0.mp4"))) == 3


def test_chunk_uri_video_udtf_default_memory_fits_geneva_field():
    # geneva serializes the Ray memory request into a signed 32-bit field;
    # a default of 2 * 1024**3 == 2**31 would OverflowError (see core.common).
    params = inspect.signature(chunkers_uri.chunk_uri_video_udtf).parameters
    assert params["memory_bytes"].default < 2**31


def test_chunk_uri_video_udtf_handles_empty_uri(monkeypatch):
    _install_fake_video_s3(monkeypatch, {})
    udtf = chunkers_uri.chunk_uri_video_udtf(chunk_seconds=1.0, manifest=None)
    assert list(udtf.func("")) == []
