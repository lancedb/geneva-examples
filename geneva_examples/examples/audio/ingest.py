"""Audio ingest CLI: seed a table of text prompts for the TTS round-trip.

Writes an ``audio`` table with an ``id`` (string) and ``text`` (string) column —
the phrases the ``synthesize`` stage turns into speech. There is no external
media: the audio is produced downstream by the TTS UDF, so the whole pipeline
(ingest → synthesize → transcribe → export) runs fully offline in local mode.
"""

from __future__ import annotations

import logging
import os

import pyarrow as pa

from geneva_examples.core.common import connect, format_sample
from geneva_examples.core.config import Config
from geneva_examples.core.utils.retry import retry_io

logger = logging.getLogger(__name__)

# (id, text) prompts seeded by default. Kept short (well under Whisper's 30s
# window) so the round-trip transcribes cleanly and runs fast on CPU.
PROMPTS: list[tuple[str, str]] = [
    ("greeting", "Hello, and welcome to the Geneva audio pipeline."),
    ("pangram", "The quick brown fox jumps over the lazy dog."),
    ("weather", "Today the sky is clear and the wind is calm."),
    ("numbers", "One, two, three, four, five, six, seven, eight, nine, ten."),
    ("cantina", "This speech was synthesized and then transcribed back to text."),
]


def run(
    cfg: Config,
    *,
    table_name: str = "audio",
    frag_size: int = 1,
    overwrite: bool = True,
    table_write_retries: int = 5,
    table_write_retry_sleep_s: float = 2.0,
) -> None:
    """Seed the ``id`` + ``text`` prompts into the table."""
    os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")

    import geneva

    logger.info("geneva_version %s mode %s", geneva.__version__, cfg.mode)
    logger.info("db_uri %s table %s", cfg.db_uri, table_name)
    logger.info("prompts %s", [pid for pid, _ in PROMPTS])

    conn = connect(cfg)

    schema = pa.schema([("id", pa.string()), ("text", pa.string())])
    batches = [
        pa.record_batch(
            {
                "id": [pid for pid, _ in PROMPTS[i : i + frag_size]],
                "text": [text for _, text in PROMPTS[i : i + frag_size]],
            },
            schema=schema,
        )
        for i in range(0, len(PROMPTS), max(1, frag_size))
    ]
    if not batches:
        raise RuntimeError("no prompts to ingest")

    if overwrite:
        try:
            conn.drop_table(table_name)
            logger.info("dropped_existing_table %s", table_name)
        except Exception:  # noqa: BLE001
            pass

    table = retry_io(
        "create_table",
        lambda: conn.create_table(table_name, data=batches[0]),
        attempts=table_write_retries,
        sleep_s=table_write_retry_sleep_s,
    )
    for batch_index, batch in enumerate(batches[1:], start=2):
        retry_io(
            f"add_batch_{batch_index}",
            lambda batch=batch: table.add(batch),
            attempts=table_write_retries,
            sleep_s=table_write_retry_sleep_s,
        )

    logger.info("rows_created %s", table.count_rows())
    try:
        logger.info("table_names %s", conn.table_names())
    except Exception:  # noqa: BLE001
        pass
    logger.info(
        "initial_sample\n%s",
        format_sample(table.search().select(["id", "text"]).limit(5).to_list()),
    )
    logger.info("ingest_audio_ok")
