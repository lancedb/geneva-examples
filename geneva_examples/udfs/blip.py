"""BLIP image-captioning UDF (H100-tuned, DataLoader-batched)."""

from __future__ import annotations

import os
import uuid

# Geneva remote runtime package pins (env-overridable for targeting other builds).
GENEVA_PACKAGE_SPEC = os.environ.get("GENEVA_PACKAGE_SPEC", "geneva==0.13.0b18")
LANCEDB_PACKAGE_SPEC = os.environ.get("LANCEDB_PACKAGE_SPEC", "lancedb==0.33.1b2")
PYLANCE_PACKAGE_SPEC = os.environ.get("PYLANCE_PACKAGE_SPEC", "pylance==8.0.0b16")
PYARROW_PACKAGE_SPEC = os.environ.get("PYARROW_PACKAGE_SPEC", "pyarrow==23.0.0")
PILLOW_PACKAGE_SPEC = os.environ.get("PILLOW_PACKAGE_SPEC", "pillow==12.2.0")
TORCH_PACKAGE_SPEC = os.environ.get("TORCH_PACKAGE_SPEC", "torch==2.12.0")
TRANSFORMERS_PACKAGE_SPEC = os.environ.get(
    "TRANSFORMERS_PACKAGE_SPEC", "transformers==5.9.0"
)

BLIP_RUNTIME_PIP = [
    GENEVA_PACKAGE_SPEC,
    LANCEDB_PACKAGE_SPEC,
    PYLANCE_PACKAGE_SPEC,
    PYARROW_PACKAGE_SPEC,
    PILLOW_PACKAGE_SPEC,
    TORCH_PACKAGE_SPEC,
    TRANSFORMERS_PACKAGE_SPEC,
]


def build_blip_caption_udf(
    *,
    input_column: str,
    manifest: object,
    batch_size: int = 256,
    num_workers: int = 8,
    num_cpus: float = 4.0,
    num_gpus: float | None = 0.5,
    memory_bytes: int = 16 * 1024**3,
    checkpoint_size: int = 4096,
    task_size: int = 4096,
    model_id: str = "Salesforce/blip-image-captioning-base",
    max_length: int = 50,
):
    """Build a BLIP captioning UDF reading ``input_column`` -> string."""
    import geneva
    import pyarrow as pa

    _batch_size, _num_workers = int(batch_size), int(num_workers)
    _model_id, _max_length = model_id, int(max_length)

    @geneva.udf(
        data_type=pa.string(),
        input_columns=[input_column],
        num_cpus=num_cpus,
        num_gpus=num_gpus,
        memory=memory_bytes,
        checkpoint_size=checkpoint_size,
        task_size=task_size,
        version=uuid.uuid4().hex,
        manifest=manifest,
    )
    class BlipCaption:
        def __init__(self):
            self.is_loaded = False
            self.logged = False
            self.batch_size = _batch_size
            self.num_workers = _num_workers
            self.model_id = _model_id
            self.max_length = _max_length

        def setup(self):
            import torch
            from transformers import BlipForConditionalGeneration, BlipProcessor

            torch.set_float32_matmul_precision("high")
            torch.backends.cuda.matmul.allow_tf32 = True
            torch.backends.cudnn.allow_tf32 = True
            self.processor = BlipProcessor.from_pretrained(self.model_id)
            self.model = BlipForConditionalGeneration.from_pretrained(self.model_id)
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            self.model.to(self.device).eval()
            self.is_loaded = True

        def __call__(self, col: pa.Array) -> pa.Array:
            import io

            import torch
            from PIL import Image
            from torch.utils.data import DataLoader, Dataset

            if not self.is_loaded:
                self.setup()

            n = len(col)
            if not self.logged:
                print(
                    "blip_udf",
                    {
                        "device": str(self.device),
                        "rows": n,
                        "batch_size": self.batch_size,
                        "num_workers": self.num_workers,
                    },
                    flush=True,
                )
                self.logged = True

            if n == 0:
                return pa.array([], type=pa.string())

            # `frame` can be null; caption only valid rows, null for the rest.
            valid_positions = [i for i in range(n) if col[i].is_valid]
            if not valid_positions:
                return pa.array([None] * n, type=pa.string())
            sub = col if len(valid_positions) == n else col.take(valid_positions)

            processor = self.processor

            class _Frames(Dataset):
                def __init__(self, arr):
                    self.arr = arr

                def __len__(self):
                    return len(self.arr)

                def __getitem__(self, i):
                    raw = self.arr[i].as_buffer().to_pybytes()
                    img = Image.open(io.BytesIO(raw)).convert("RGB")
                    # Run the resize/normalize processor in the worker proc, not
                    # serially on the task's main thread. Captioning is
                    # unconditional, so pixel_values is the only model input.
                    return processor(img, return_tensors="pt").pixel_values[0]

            loader_kwargs = dict(
                batch_size=self.batch_size,
                num_workers=self.num_workers,
                pin_memory=(self.device.type == "cuda"),
                collate_fn=torch.stack,
            )
            if self.num_workers > 0:
                loader_kwargs["prefetch_factor"] = 4
            loader = DataLoader(_Frames(sub), **loader_kwargs)

            captions: list[str] = []
            autocast = self.device.type == "cuda"
            with (
                torch.inference_mode(),
                torch.autocast(
                    device_type="cuda", dtype=torch.bfloat16, enabled=autocast
                ),
            ):
                for pixel_values in loader:
                    pixel_values = pixel_values.to(self.device, non_blocking=True)
                    output_ids = self.model.generate(
                        pixel_values=pixel_values,
                        num_beams=1,
                        do_sample=False,
                        use_cache=True,
                        max_new_tokens=self.max_length,
                    )
                    captions.extend(
                        self.processor.batch_decode(
                            output_ids, skip_special_tokens=True
                        )
                    )

            if len(valid_positions) == n:
                return pa.array(captions, type=pa.string())
            # Scatter captions back into full-length output with nulls.
            rows: list[object] = [None] * n
            for k, pos in enumerate(valid_positions):
                rows[pos] = captions[k]
            return pa.array(rows, type=pa.string())

    return BlipCaption()
