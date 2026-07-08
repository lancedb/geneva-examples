"""OpenCLIP image-embedding UDF (H100-tuned, DataLoader-batched)."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from typing import Any

from geneva_examples.core.package_specs import package_spec

# Geneva remote runtime package pins (env-overridable for targeting other builds).
# geneva/lancedb/pylance track the installed versions so the workers match the
# client's locked env; the rest stay exact-pinned for reproducible worker builds.
GENEVA_PACKAGE_SPEC = package_spec("geneva")
LANCEDB_PACKAGE_SPEC = package_spec("lancedb")
PYLANCE_PACKAGE_SPEC = package_spec("pylance")
PYARROW_PACKAGE_SPEC = os.environ.get("PYARROW_PACKAGE_SPEC", "pyarrow==23.0.0")
PILLOW_PACKAGE_SPEC = os.environ.get("PILLOW_PACKAGE_SPEC", "pillow==12.2.0")
NUMPY_PACKAGE_SPEC = os.environ.get("NUMPY_PACKAGE_SPEC", "numpy==2.4.6")
TORCH_PACKAGE_SPEC = os.environ.get("TORCH_PACKAGE_SPEC", "torch==2.12.0")
OPEN_CLIP_PACKAGE_SPEC = os.environ.get(
    "OPEN_CLIP_PACKAGE_SPEC", "open-clip-torch==3.3.0"
)

CLIP_RUNTIME_PIP = [
    GENEVA_PACKAGE_SPEC,
    LANCEDB_PACKAGE_SPEC,
    PYLANCE_PACKAGE_SPEC,
    PYARROW_PACKAGE_SPEC,
    PILLOW_PACKAGE_SPEC,
    NUMPY_PACKAGE_SPEC,
    TORCH_PACKAGE_SPEC,
    OPEN_CLIP_PACKAGE_SPEC,
]


def build_clip_embedding_udf(
    *,
    input_column: str,
    manifest: Any,
    batch_size: int = 256,
    num_workers: int = 8,
    num_cpus: float = 4.0,
    num_gpus: float | None = 0.5,
    memory_bytes: int = 16 * 1024**3,
    checkpoint_size: int = 4096,
    task_size: int = 4096,
    model_name: str = "ViT-B-32",
    pretrained: str = "laion2b_s34b_b79k",
    dim: int = 512,
):
    """Build an OpenCLIP image-embedding UDF reading ``input_column``."""
    import geneva
    import pyarrow as pa

    _batch_size, _num_workers = int(batch_size), int(num_workers)
    _model_name, _pretrained, _dim = model_name, pretrained, int(dim)

    @geneva.udf(
        data_type=pa.list_(pa.float32(), _dim),
        input_columns=[input_column],
        num_cpus=num_cpus,
        num_gpus=num_gpus,
        memory=memory_bytes,
        checkpoint_size=checkpoint_size,
        task_size=task_size,
        version=uuid.uuid4().hex,
        manifest=manifest,
    )
    class ClipEmbedding(Callable):
        def __init__(self):
            self.is_loaded = False
            self.logged = False
            self.batch_size = _batch_size
            self.num_workers = _num_workers
            self.model_name = _model_name
            self.pretrained = _pretrained
            self.dim = _dim

        def setup(self):
            import open_clip
            import torch

            torch.set_float32_matmul_precision("high")
            torch.backends.cuda.matmul.allow_tf32 = True
            torch.backends.cudnn.allow_tf32 = True
            self.model, _, self.preprocess = open_clip.create_model_and_transforms(
                self.model_name, pretrained=self.pretrained
            )
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
            self.model = self.model.to(self.device).eval()
            if self.device == "cuda":
                self.model = self.model.to(memory_format=torch.channels_last)
                # Compile the ViT forward pass; warmup cost amortizes over the
                # backfill. Fall back silently if the runtime can't compile.
                try:
                    self.model = torch.compile(self.model)
                except Exception as exc:  # noqa: BLE001
                    print("clip_compile_skip", type(exc).__name__, flush=True)
            self.is_loaded = True

        def __call__(self, col: pa.Array) -> pa.Array:
            # Runs in the remote Geneva runtime; print -> remote worker stdout.
            import io

            import numpy as np
            import torch
            from PIL import Image
            from torch.utils.data import DataLoader, Dataset

            if not self.is_loaded:
                self.setup()

            n = len(col)
            if not self.logged:
                print(
                    "clip_udf",
                    {
                        "device": self.device,
                        "cuda": torch.cuda.is_available(),
                        "rows": n,
                        "batch_size": self.batch_size,
                        "num_workers": self.num_workers,
                    },
                    flush=True,
                )
                self.logged = True

            if n == 0:
                return pa.FixedSizeListArray.from_arrays(
                    pa.array([], type=pa.float32()), self.dim
                )

            # `frame` can be null; embed only valid rows and emit null for the
            # rest (a fixed-size list can't be built from a ragged input).
            valid_positions = [i for i in range(n) if col[i].is_valid]
            if not valid_positions:
                return pa.array([None] * n, type=pa.list_(pa.float32(), self.dim))
            sub = col if len(valid_positions) == n else col.take(valid_positions)

            preprocess = self.preprocess

            class _Frames(Dataset):
                def __init__(self, arr):
                    self.arr = arr

                def __len__(self):
                    return len(self.arr)

                def __getitem__(self, i):  # ty: ignore[invalid-method-override]  # third-party stub gap
                    raw = self.arr[i].as_buffer().to_pybytes()
                    img = Image.open(io.BytesIO(raw)).convert("RGB")
                    return preprocess(img)

            loader_kwargs: dict[str, Any] = dict(
                batch_size=self.batch_size,
                num_workers=self.num_workers,
                pin_memory=(self.device == "cuda"),
                collate_fn=torch.stack,
            )
            if self.num_workers > 0:
                # Prefetch upcoming batches in the worker procs so PIL decode +
                # preprocess overlaps GPU compute (needs batch_size < rows/call).
                loader_kwargs["prefetch_factor"] = 4
            loader = DataLoader(_Frames(sub), **loader_kwargs)

            chunks = []
            autocast = self.device == "cuda"
            with (
                torch.inference_mode(),
                torch.autocast(
                    device_type="cuda", dtype=torch.bfloat16, enabled=autocast
                ),
            ):
                for batch in loader:
                    batch = batch.to(self.device, non_blocking=True)
                    if self.device == "cuda":
                        batch = batch.to(memory_format=torch.channels_last)
                    emb = self.model.encode_image(batch)
                    emb = emb / emb.norm(dim=-1, keepdim=True)
                    # Keep on-GPU; one device->host copy after the loop instead
                    # of a per-batch sync.
                    chunks.append(emb.float())

            out = torch.cat(chunks).cpu().numpy().astype(np.float32)
            if len(valid_positions) == n:
                return pa.FixedSizeListArray.from_arrays(
                    pa.array(out.reshape(-1), type=pa.float32()), self.dim
                )
            # Scatter embeddings back into full-length output with nulls.
            rows: list[object] = [None] * n
            for k, pos in enumerate(valid_positions):
                rows[pos] = out[k].tolist()
            return pa.array(rows, type=pa.list_(pa.float32(), self.dim))

    return ClipEmbedding()
