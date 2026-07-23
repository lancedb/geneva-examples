"""Audio round-trip pipeline — a self-contained example.

Seed a table of text prompts, synthesize speech from them with a
transformers-native MMS-TTS UDF (auto-downloading, no model files), transcribe the
audio back to text with Geneva's shipped Whisper UDF, then export the synthesized
waveforms to .wav files. A text -> speech -> text round-trip that runs fully
offline in local mode.

Order: **ingest-audio -> synthesize-audio -> transcribe-audio -> export-audio**.
"""

from __future__ import annotations

from geneva_examples.core.spec import (
    COMMON_HELP,
    Example,
    Step,
    params_from_signature,
)
from geneva_examples.examples.audio import (
    export,
    ingest,
    synthesize,
    transcribe,
)

INGEST = Step(
    key="ingest-audio",
    title="Ingest text prompts",
    description=(
        "Seed an `audio` table with `id` + `text` prompts — the phrases the "
        "synthesize step turns into speech. No external media; the pipeline runs "
        "fully offline in local mode."
    ),
    run=ingest.run,
    params=params_from_signature(ingest.run, help=COMMON_HELP),
)

SYNTHESIZE = Step(
    key="synthesize-audio",
    title="MMS-TTS speech synthesis",
    description=(
        "Backfill an `audio` waveform column (list<float32> @ 16 kHz) from `text` "
        "using a transformers-native MMS-TTS UDF (facebook/mms-tts-eng). The model "
        "auto-downloads to the HF cache on first run — no model files needed."
    ),
    run=synthesize.run,
    gpu=True,
    requires="run ingest-audio first",
    params=params_from_signature(
        synthesize.run,
        help=COMMON_HELP
        | {
            "reset": (
                "Drop and re-synthesize the whole audio column (destructive). "
                "Default off = incremental: only synthesize rows still missing it."
            ),
        },
    ),
)

TRANSCRIBE = Step(
    key="transcribe-audio",
    title="Whisper transcription",
    description=(
        "Backfill a `transcript` column by transcribing the synthesized `audio` "
        "column with Geneva's shipped WhisperChunkTranscriber "
        "(openai/whisper-large-v3-turbo by default, auto-downloads; override with "
        "--model-id). Closes the text -> speech -> text round-trip."
    ),
    run=transcribe.run,
    gpu=True,
    requires="run synthesize-audio first",
    params=params_from_signature(
        transcribe.run,
        help=COMMON_HELP
        | {
            "model_id": (
                "HF ASR checkpoint (e.g. openai/whisper-tiny .. "
                "openai/whisper-large-v3-turbo); auto-downloads on first run."
            ),
            "reset": (
                "Drop and re-transcribe the whole transcript column (destructive). "
                "Default off = incremental: only transcribe rows still missing it."
            ),
        },
    ),
)

EXPORT = Step(
    key="export-audio",
    title="Export waveforms to .wav",
    description=(
        "Write each row's synthesized `audio` waveform to a 16-bit PCM WAV file "
        "under `--out-dir` (default /tmp/geneva_audio), named `<id>.wav`. Runs on "
        "the client; needs no extra audio dependency."
    ),
    run=export.run,
    requires="run synthesize-audio first",
    params=params_from_signature(
        export.run,
        help=COMMON_HELP
        | {
            "audio_column": "Waveform column to export.",
            "id_column": "Column used for each output filename.",
            "out_dir": "Directory to write .wav files into.",
            "sample_rate": "WAV sample rate (Hz); MMS-TTS emits 16000.",
            "limit": "Max rows to export (0 = all).",
        },
        bounds={"limit": (0, None)},
    ),
)

EXAMPLE = Example(
    name="audio",
    title="Audio round-trip pipeline",
    description=(
        "Seed text prompts, synthesize speech with MMS-TTS, transcribe it back "
        "with Whisper, and export the waveforms to .wav — a text -> speech -> text "
        "round-trip that runs offline with auto-downloading models.\n\n"
        "Order: **ingest-audio -> synthesize-audio -> transcribe-audio -> "
        "export-audio**."
    ),
    modality="audio",
    steps=(INGEST, SYNTHESIZE, TRANSCRIBE, EXPORT),
)
