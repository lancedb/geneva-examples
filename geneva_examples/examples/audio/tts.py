"""MMS-TTS text-to-speech UDF (transformers-native, auto-downloading).

Synthesizes speech from a ``text`` column with Meta's MMS-TTS English checkpoint
(``facebook/mms-tts-eng``), a VITS model that ``transformers`` loads directly via
``VitsModel``/``VitsTokenizer``. The weights **auto-download and cache** to the HF
cache on first use — no pre-placed model files and no system binaries (unlike the
ONNX ``kokoro_base_tts_udf``, which needs a local ``model_dir``, or the ``kokoro``
pip package, which needs ``espeak-ng``). English is Roman-alphabet, so the
tokenizer needs no ``uroman`` preprocessing.

Output is a variable-length ``list<float32>`` waveform at **16 kHz** — recorded in
the column's ``field_metadata`` as ``sample_rate_hz`` — which is exactly what the
downstream ``whisper-tiny`` transcription stage expects, so the two chain with no
resampling. Mirrors the factory + decorated-class shape of
:mod:`geneva_examples.examples._shared.clip`.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from typing import Any

from geneva_examples.core.package_specs import package_spec

# The output waveform sample rate. MMS-TTS English emits 16 kHz, matching the
# Whisper transcriber's TARGET_SAMPLE_RATE — verified at setup() against the
# loaded model's config.sampling_rate.
MMS_TTS_SAMPLE_RATE = 16_000

MMS_TTS_MODEL_ID = os.environ.get("MMS_TTS_MODEL_ID", "facebook/mms-tts-eng")

# Geneva remote runtime package pins (env-overridable for targeting other builds).
# geneva/lancedb/pylance track the installed versions so the workers match the
# client's locked env; the rest stay exact-pinned for reproducible worker builds.
GENEVA_PACKAGE_SPEC = package_spec("geneva")
LANCEDB_PACKAGE_SPEC = package_spec("lancedb")
PYLANCE_PACKAGE_SPEC = package_spec("pylance")
PYARROW_PACKAGE_SPEC = os.environ.get("PYARROW_PACKAGE_SPEC", "pyarrow==23.0.0")
NUMPY_PACKAGE_SPEC = os.environ.get("NUMPY_PACKAGE_SPEC", "numpy==2.4.6")
TORCH_PACKAGE_SPEC = os.environ.get("TORCH_PACKAGE_SPEC", "torch==2.12.0")
TRANSFORMERS_PACKAGE_SPEC = os.environ.get(
    "TRANSFORMERS_PACKAGE_SPEC", "transformers==5.0.0"
)

MMS_TTS_RUNTIME_PIP = [
    GENEVA_PACKAGE_SPEC,
    LANCEDB_PACKAGE_SPEC,
    PYLANCE_PACKAGE_SPEC,
    PYARROW_PACKAGE_SPEC,
    NUMPY_PACKAGE_SPEC,
    TORCH_PACKAGE_SPEC,
    TRANSFORMERS_PACKAGE_SPEC,
]


def build_mms_tts_udf(
    *,
    text_column: str = "text",
    manifest: Any,
    num_cpus: float = 4.0,
    num_gpus: float | None = 0.5,
    memory_bytes: int = 4 * 1024**3,
    checkpoint_size: int = 32,
    task_size: int = 32,
    model_id: str = MMS_TTS_MODEL_ID,
):
    """Build an MMS-TTS UDF: ``text_column`` (string) -> ``list<float32>`` @ 16 kHz.

    The model auto-downloads to the HF cache on first ``setup()``. Null/empty text
    rows yield null output. The output column carries ``sample_rate_hz`` in its
    field metadata so downstream consumers (transcription, export) know the rate.
    """
    import geneva
    import pyarrow as pa

    _model_id = model_id

    @geneva.udf(
        data_type=pa.list_(pa.float32()),
        input_columns=[text_column],
        num_cpus=num_cpus,
        num_gpus=num_gpus,
        memory=memory_bytes,
        checkpoint_size=checkpoint_size,
        task_size=task_size,
        field_metadata={"sample_rate_hz": str(MMS_TTS_SAMPLE_RATE)},
        version=uuid.uuid4().hex,
        manifest=manifest,
    )
    class MmsTts(Callable):
        def __init__(self):
            self.is_loaded = False
            self.logged = False
            self.model_id = _model_id

        def setup(self):
            import torch
            from transformers import VitsModel, VitsTokenizer

            self.tokenizer = VitsTokenizer.from_pretrained(self.model_id)
            self.model = VitsModel.from_pretrained(self.model_id)
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
            model = self.model.to(self.device)  # ty: ignore[invalid-argument-type]  # transformers .to() stub gap
            self.model = model.eval()
            rate = int(self.model.config.sampling_rate)
            if rate != MMS_TTS_SAMPLE_RATE:
                # Surface a mismatch loudly: the downstream Whisper stage assumes
                # 16 kHz samples, so a different rate would silently corrupt the
                # transcript. Callers can set MMS_TTS_MODEL_ID to a 16 kHz model.
                raise RuntimeError(
                    f"{self.model_id} emits {rate} Hz, expected "
                    f"{MMS_TTS_SAMPLE_RATE} Hz (downstream Whisper assumes 16 kHz)"
                )
            self.is_loaded = True

        def __call__(self, col: pa.Array) -> pa.Array:
            # Runs in the remote Geneva runtime; print -> remote worker stdout.
            import torch

            if not self.is_loaded:
                self.setup()

            n = len(col)
            if not self.logged:
                print(
                    "mms_tts_udf",
                    {"device": self.device, "rows": n, "model": self.model_id},
                    flush=True,
                )
                self.logged = True

            out_type = pa.list_(pa.float32())
            if n == 0:
                return pa.array([], type=out_type)

            rows: list[list[float] | None] = [None] * n
            with torch.inference_mode():
                for i in range(n):
                    scalar = col[i]
                    if not scalar.is_valid:
                        continue
                    text = scalar.as_py()
                    if not isinstance(text, str) or not text.strip():
                        continue
                    inputs = self.tokenizer(text=text, return_tensors="pt").to(
                        self.device
                    )
                    waveform = self.model(**inputs).waveform[0]
                    rows[i] = waveform.detach().to("cpu").float().numpy().tolist()

            return pa.array(rows, type=out_type)

    return MmsTts()
