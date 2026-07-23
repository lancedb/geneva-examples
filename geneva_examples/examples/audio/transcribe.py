"""Transcribe step: Whisper speech-to-text on the synthesized audio (GPU/CPU).

Closes the text -> speech -> text round-trip by transcribing the ``audio`` column
(produced by the ``synthesize`` stage) back into a ``transcript`` column, reusing
the ``WhisperChunkTranscriber`` UDF Geneva ships in ``geneva.udfs.audio``.

Like ``examples/pdf/document.py`` reuses ``geneva.udfs.document``, we don't write a
new UDF body: we instantiate the shipped class UDF and ``attrs.evolve`` a copy with
this repo's pinned manifest, a fresh ``version``, and its input column rebound to
``audio`` (it defaults to ``samples``). Whisper defaults to
``openai/whisper-large-v3-turbo`` (override with ``--model-id``), which
auto-downloads/caches via ``transformers``. The TTS output is already
``list<float32>`` @ 16 kHz — exactly what the transcriber expects — so it feeds in
directly, with no ``download_audio`` and no resampling.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from geneva_examples.core.backfill import backfill_column
from geneva_examples.core.common import (
    build_manifest,
    connect,
    format_sample,
    local_or,
    resolve_resources,
    runtime_session,
)
from geneva_examples.core.config import Config
from geneva_examples.core.package_specs import package_spec

logger = logging.getLogger(__name__)

# Whisper runs on torch + transformers, both already in the client env. Pinned
# here so remote workers install the versions the client locked.
GENEVA_PACKAGE_SPEC = package_spec("geneva")
LANCEDB_PACKAGE_SPEC = package_spec("lancedb")
PYLANCE_PACKAGE_SPEC = package_spec("pylance")
PYARROW_PACKAGE_SPEC = os.environ.get("PYARROW_PACKAGE_SPEC", "pyarrow==23.0.0")
NUMPY_PACKAGE_SPEC = os.environ.get("NUMPY_PACKAGE_SPEC", "numpy==2.4.6")
TORCH_PACKAGE_SPEC = os.environ.get("TORCH_PACKAGE_SPEC", "torch==2.12.0")
TRANSFORMERS_PACKAGE_SPEC = os.environ.get(
    "TRANSFORMERS_PACKAGE_SPEC", "transformers==5.0.0"
)

WHISPER_RUNTIME_PIP = [
    GENEVA_PACKAGE_SPEC,
    LANCEDB_PACKAGE_SPEC,
    PYLANCE_PACKAGE_SPEC,
    PYARROW_PACKAGE_SPEC,
    NUMPY_PACKAGE_SPEC,
    TORCH_PACKAGE_SPEC,
    TRANSFORMERS_PACKAGE_SPEC,
]


def build_whisper_transcribe_udf(
    *,
    audio_column: str,
    manifest: Any,
    model_id: str,
    num_cpus: float,
    num_gpus: float | None,
    memory_bytes: int,
):
    """Build the ``WhisperChunkTranscriber`` UDF rebound to ``audio_column``.

    Reuses ``geneva.udfs.audio.WhisperChunkTranscriber`` (instantiated with
    ``model_id``) with this repo's manifest, a fresh ``version``, its input column
    rebound from the default ``samples`` to ``audio_column``, and the mode-resolved
    Ray resources. Any ``transformers`` ASR checkpoint works (e.g.
    ``openai/whisper-tiny`` … ``openai/whisper-large-v3-turbo``); it auto-downloads
    on first run. Input: ``list<float32>`` @ 16 kHz; output: ``large_string``.
    """
    import attrs
    from geneva.udfs.audio import WhisperChunkTranscriber

    return attrs.evolve(
        WhisperChunkTranscriber(model_id=model_id),
        input_columns=[audio_column],
        num_cpus=num_cpus,
        num_gpus=num_gpus,
        memory=memory_bytes,
        manifest=manifest,
        version=uuid.uuid4().hex,
    )


def run(
    cfg: Config,
    *,
    table_name: str = "audio",
    input_column: str = "audio",
    output_column: str = "transcript",
    model_id: str = "openai/whisper-large-v3-turbo",
    num_cpus: float = 4.0,
    num_gpus: float | None = None,
    memory_gib: int = 4,
    checkpoint_size: int = 32,
    task_size: int = 32,
    concurrency: int = 16,
    backfill_timeout_min: int = 1000,
    flush_interval_s: float = 30.0,
    schema_wait_attempts: int = 30,
    schema_wait_sleep_s: int = 2,
    reset: bool = False,
) -> None:
    """Add a Whisper ``transcript`` column from the synthesized ``audio`` column.

    Run after ``synthesize``. Incremental by default; pass ``reset=True`` to
    re-transcribe every row.
    """
    os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")
    os.environ.setdefault("HF_HOME", "./huggingface_cache")

    import geneva

    resolved_gpus = num_gpus if num_gpus is not None else 0.5
    num_cpus, resolved_gpus, memory_bytes = resolve_resources(
        cfg, num_cpus=num_cpus, num_gpus=resolved_gpus, memory_gib=memory_gib
    )
    concurrency = local_or(cfg, 1, concurrency)

    logger.info("geneva_version %s mode %s", geneva.__version__, cfg.mode)
    logger.info("db_uri %s table %s column %s", cfg.db_uri, table_name, input_column)
    logger.info(
        "model_id %s num_cpus %s num_gpus %s", model_id, num_cpus, resolved_gpus
    )

    conn = connect(cfg)
    table = conn.open_table(table_name)

    manifest = build_manifest(cfg, "audio-transcribe", WHISPER_RUNTIME_PIP)
    udf = build_whisper_transcribe_udf(
        audio_column=input_column,
        manifest=manifest,
        model_id=model_id,
        num_cpus=num_cpus,
        num_gpus=resolved_gpus,
        memory_bytes=memory_bytes,
    )
    with runtime_session(conn, cfg):
        table = backfill_column(
            conn=conn,
            table=table,
            table_name=table_name,
            column=output_column,
            udf=udf,
            concurrency=concurrency,
            task_size=task_size,
            checkpoint_size=checkpoint_size,
            flush_interval_s=flush_interval_s,
            timeout_min=backfill_timeout_min,
            wait_attempts=schema_wait_attempts,
            wait_sleep_s=schema_wait_sleep_s,
            reset=reset,
        )

    logger.info(
        "transcript_sample\n%s",
        format_sample(
            table.search().select(["id", "text", output_column]).limit(5).to_list()
        ),
    )
    logger.info("transcribe_audio_ok")
