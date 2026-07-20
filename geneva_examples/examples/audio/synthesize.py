"""Synthesize step: MMS-TTS speech waveforms on audio.text (GPU/CPU).

Backfills an ``audio`` column (``list<float32>`` @ 16 kHz) from the ``text``
column using the transformers-native MMS-TTS UDF. The model auto-downloads to the
HF cache on first run; nothing needs to be pre-placed. The ``audio`` column is the
input to both the transcription and export stages.
"""

from __future__ import annotations

import logging
import os

from geneva_examples.core.backfill import backfill_column
from geneva_examples.core.common import (
    build_manifest,
    connect,
    local_or,
    resolve_resources,
    runtime_session,
)
from geneva_examples.core.config import Config

logger = logging.getLogger(__name__)


def run(
    cfg: Config,
    *,
    table_name: str = "audio",
    input_column: str = "text",
    output_column: str = "audio",
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
    """Add an MMS-TTS ``audio`` waveform column to the audio table.

    Incremental by default (only synthesizes rows whose ``audio`` is still null),
    so a partial run can be re-run cheaply. Pass ``reset=True`` (``--reset``) to
    drop and re-synthesize every row (e.g. after changing the model).
    """
    os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")
    os.environ.setdefault("HF_HOME", "./huggingface_cache")

    import geneva

    from geneva_examples.examples.audio.tts import (
        MMS_TTS_RUNTIME_PIP,
        build_mms_tts_udf,
    )

    resolved_gpus = num_gpus if num_gpus is not None else 0.5
    num_cpus, resolved_gpus, memory_bytes = resolve_resources(
        cfg, num_cpus=num_cpus, num_gpus=resolved_gpus, memory_gib=memory_gib
    )
    concurrency = local_or(cfg, 1, concurrency)

    logger.info("geneva_version %s mode %s", geneva.__version__, cfg.mode)
    logger.info("db_uri %s table %s column %s", cfg.db_uri, table_name, input_column)
    logger.info("num_cpus %s num_gpus %s", num_cpus, resolved_gpus)

    conn = connect(cfg)
    table = conn.open_table(table_name)

    manifest = build_manifest(cfg, "audio-tts", MMS_TTS_RUNTIME_PIP)
    udf = build_mms_tts_udf(
        text_column=input_column,
        manifest=manifest,
        num_cpus=num_cpus,
        num_gpus=resolved_gpus,
        memory_bytes=memory_bytes,
        checkpoint_size=checkpoint_size,
        task_size=task_size,
    )
    with runtime_session(conn, cfg):
        backfill_column(
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
    logger.info("synthesize_audio_ok")
