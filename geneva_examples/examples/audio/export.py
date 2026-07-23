"""Export step: write the synthesized ``audio`` waveforms to .wav files.

Reads the ``audio`` column (``list<float32>`` @ 16 kHz, produced by the
``synthesize`` stage) and writes one 16-bit PCM WAV per row to ``--out-dir``
(``/tmp`` by default), named ``<id>.wav``. Uses the stdlib ``wave`` module, so it
needs no extra audio dependency.

This runs entirely on the client (a plain table scan + file writes), so it works
unchanged in both local and enterprise modes — in enterprise mode the waveform
bytes are pulled back from the cluster to write locally.
"""

from __future__ import annotations

import logging
import os
import wave
from pathlib import Path

from geneva_examples.core.common import connect
from geneva_examples.core.config import Config

logger = logging.getLogger(__name__)


def _write_wav(path: Path, samples: list[float], sample_rate: int) -> None:
    """Write float samples in [-1, 1] as a mono 16-bit PCM WAV at ``sample_rate``."""
    import numpy as np

    arr = np.asarray(samples, dtype=np.float32)
    # Clip to guard against any out-of-range values, then scale to int16.
    arr = np.clip(arr, -1.0, 1.0)
    pcm = (arr * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)  # 16-bit
        wav.setframerate(sample_rate)
        wav.writeframes(pcm.tobytes())


def run(
    cfg: Config,
    *,
    table_name: str = "audio",
    audio_column: str = "audio",
    id_column: str = "id",
    out_dir: str = "/tmp/geneva_audio",
    sample_rate: int = 16_000,
    limit: int = 0,
) -> None:
    """Export the ``audio`` column to ``<out-dir>/<id>.wav`` files.

    ``--limit 0`` exports every row; a positive value caps how many are written.
    Rows with a null ``audio`` value (not yet synthesized) are skipped.
    """
    os.environ.setdefault("RAY_ENABLE_UV_RUN_RUNTIME_ENV", "0")

    import geneva

    logger.info("geneva_version %s mode %s", geneva.__version__, cfg.mode)
    logger.info("db_uri %s table %s out_dir %s", cfg.db_uri, table_name, out_dir)

    conn = connect(cfg)
    table = conn.open_table(table_name)

    if audio_column not in set(table.schema.names):
        raise RuntimeError(
            f"table {table_name!r} has no {audio_column!r} column — "
            "run the synthesize step first"
        )

    out_path = Path(out_dir).expanduser()
    out_path.mkdir(parents=True, exist_ok=True)

    query = table.search().select([id_column, audio_column])
    if limit and limit > 0:
        query = query.limit(limit)
    rows = query.to_list()

    written = 0
    skipped = 0
    for row in rows:
        samples = row.get(audio_column)
        if not samples:
            skipped += 1
            continue
        row_id = str(row.get(id_column) or f"row-{written}")
        # Keep the filename filesystem-safe.
        safe_id = "".join(c if c.isalnum() or c in "-_." else "_" for c in row_id)
        dest = out_path / f"{safe_id}.wav"
        _write_wav(dest, samples, sample_rate)
        logger.info("wrote %s (%d samples)", dest, len(samples))
        written += 1

    logger.info(
        "export_audio_ok wrote %d skipped %d dir %s", written, skipped, out_path
    )
