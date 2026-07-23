"""Audio round-trip pipeline: UDF factories, the WAV export helper, and CLI wiring.

The TTS/transcribe backfill bodies need Ray + model weights (excluded from the
coverage gate), but their *factories* build real Geneva UDF objects on the driver,
so we assert their binding (input column, data type, metadata, manifest). The
export helper is pure and round-trips through the stdlib ``wave`` reader. The
ingest/export CLIs are driven through ``CliRunner`` with the cluster faked.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pyarrow as pa
import pytest
from _fakes import FakeConn, FakeTable
from click.testing import CliRunner

from geneva_examples.examples import cli
from geneva_examples.examples.audio import export as export_mod
from geneva_examples.examples.audio import transcribe as transcribe_mod
from geneva_examples.examples.audio import tts as tts_mod

# --- UDF factories ----------------------------------------------------------


def test_mms_tts_factory_binds_text_to_waveform():
    udf = tts_mod.build_mms_tts_udf(text_column="text", manifest=None)
    assert udf.input_columns == ["text"]
    assert udf.data_type == pa.list_(pa.float32())
    # Sample rate is recorded in the column metadata for downstream stages.
    assert udf.field_metadata == {"sample_rate_hz": str(tts_mod.MMS_TTS_SAMPLE_RATE)}
    # Fresh version each build so a re-run re-materializes the column.
    assert (
        udf.version
        != tts_mod.build_mms_tts_udf(text_column="text", manifest=None).version
    )


def test_whisper_factory_rebinds_input_and_model():
    from geneva.manifest import GenevaManifest

    manifest = (
        GenevaManifest.create_pip("whisper-test")
        .pip(transcribe_mod.WHISPER_RUNTIME_PIP)
        .build()
    )
    udf = transcribe_mod.build_whisper_transcribe_udf(
        audio_column="audio",
        manifest=manifest,
        model_id="openai/whisper-tiny",
        num_cpus=2.0,
        num_gpus=0,
        memory_bytes=1024**3,
    )
    # Rebound from the shipped default input column ("samples") to "audio".
    assert udf.input_columns == ["audio"]
    assert udf.data_type == pa.large_string()
    assert udf.manifest is manifest
    assert udf.num_gpus == 0


# --- WAV export helper ------------------------------------------------------


def test_write_wav_roundtrips_as_pcm16(tmp_path: Path):
    dest = tmp_path / "s.wav"
    export_mod._write_wav(dest, [0.0, 1.0, -1.0], sample_rate=16_000)

    with wave.open(str(dest), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2  # 16-bit
        assert w.getframerate() == 16_000
        assert w.getnframes() == 3
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    assert pcm[0] == 0
    assert pcm[1] == 32767  # +1.0 -> full scale
    assert pcm[2] == -32767  # -1.0 -> full scale


def test_write_wav_clips_out_of_range(tmp_path: Path):
    dest = tmp_path / "c.wav"
    export_mod._write_wav(dest, [2.0, -2.0], sample_rate=16_000)
    with wave.open(str(dest), "rb") as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    assert pcm[0] == 32767 and pcm[1] == -32767


# --- CLI smoke tests --------------------------------------------------------


def test_ingest_audio_creates_and_seeds(monkeypatch: pytest.MonkeyPatch):
    from geneva_examples.examples.audio import ingest as mod

    conn = FakeConn(table=FakeTable(names=["id", "text"]))
    monkeypatch.setattr(mod, "connect", lambda _cfg: conn)

    result = CliRunner().invoke(cli.ingest_audio, ["--mode", "local"])

    assert result.exit_code == 0, result.output
    assert "audio" in conn.created  # created the default table
    assert "audio" in conn.dropped  # overwrite=True dropped it first
    # 5 default prompts, frag_size=1 -> create_table(batch0) + 4 appends.
    assert len(conn.created["audio"].adds) == 4


class _RowsTable(FakeTable):
    """FakeTable whose scan returns fixed rows (one with audio, one null)."""

    def to_list(self):
        return [
            {"id": "a", "audio": [0.0, 0.5, -0.5]},
            {"id": "b", "audio": None},  # not yet synthesized -> skipped
        ]


def test_export_audio_writes_wavs_and_skips_null(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from geneva_examples.examples.audio import export as mod

    conn = FakeConn(table=_RowsTable(names=["id", "audio"]))
    monkeypatch.setattr(mod, "connect", lambda _cfg: conn)
    out = tmp_path / "wavs"

    result = CliRunner().invoke(
        cli.export_audio, ["--mode", "local", "--out-dir", str(out)]
    )

    assert result.exit_code == 0, result.output
    assert (out / "a.wav").exists()
    assert not (out / "b.wav").exists()  # null audio skipped


def test_export_audio_errors_without_audio_column(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from geneva_examples.examples.audio import export as mod

    conn = FakeConn(table=FakeTable(names=["id", "text"]))  # no audio column
    monkeypatch.setattr(mod, "connect", lambda _cfg: conn)

    result = CliRunner().invoke(
        cli.export_audio, ["--mode", "local", "--out-dir", str(tmp_path / "wavs")]
    )

    assert result.exit_code != 0
    assert "run the synthesize step first" in str(result.exception)
