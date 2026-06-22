"""OpenPose pose-skeleton UDF (controlnet_aux, DataLoader-batched decode)."""

from __future__ import annotations

import os
import uuid

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
CONTROLNET_AUX_PACKAGE_SPEC = os.environ.get(
    "CONTROLNET_AUX_PACKAGE_SPEC", "controlnet-aux>=0.0.7"
)

OPENPOSE_RUNTIME_PIP = [
    GENEVA_PACKAGE_SPEC,
    LANCEDB_PACKAGE_SPEC,
    PYLANCE_PACKAGE_SPEC,
    PYARROW_PACKAGE_SPEC,
    PILLOW_PACKAGE_SPEC,
    NUMPY_PACKAGE_SPEC,
    TORCH_PACKAGE_SPEC,
    CONTROLNET_AUX_PACKAGE_SPEC,
]


def build_openpose_udf(
    *,
    input_column: str,
    manifest: object,
    batch_size: int = 256,
    num_workers: int = 4,
    num_cpus: float = 2.0,
    num_gpus: float | None = 0.25,
    memory_bytes: int = 16 * 1024**3,
    checkpoint_size: int = 4096,
    task_size: int = 4096,
    detector_repo: str = "lllyasviel/Annotators",
    include_hand: bool = True,
    include_face: bool = True,
):
    """Build a pose-skeleton UDF reading ``input_column`` -> PNG bytes."""
    import geneva
    import pyarrow as pa

    _batch_size, _num_workers = int(batch_size), int(num_workers)
    _repo, _hand, _face = detector_repo, bool(include_hand), bool(include_face)

    @geneva.udf(
        data_type=pa.large_binary(),
        input_columns=[input_column],
        num_cpus=num_cpus,
        num_gpus=num_gpus,
        memory=memory_bytes,
        checkpoint_size=checkpoint_size,
        task_size=task_size,
        version=uuid.uuid4().hex,
        manifest=manifest,
    )
    class OpenPose:
        def __init__(self):
            self.is_loaded = False
            self.logged = False
            self.batch_size = _batch_size
            self.num_workers = _num_workers
            self.repo = _repo
            self.include_hand = _hand
            self.include_face = _face

        def setup(self):
            import torch
            from controlnet_aux import OpenposeDetector

            self.detector = OpenposeDetector.from_pretrained(self.repo)
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
            try:
                self.detector = self.detector.to(self.device)
            except Exception:  # noqa: BLE001
                pass
            self.is_loaded = True

        def __call__(self, col: pa.Array) -> pa.Array:
            import io

            from PIL import Image
            from torch.utils.data import DataLoader, Dataset

            if not self.is_loaded:
                self.setup()

            n = len(col)
            if not self.logged:
                print(
                    "openpose_udf",
                    {
                        "device": self.device,
                        "rows": n,
                        "batch_size": self.batch_size,
                        "num_workers": self.num_workers,
                        "repo": self.repo,
                    },
                    flush=True,
                )
                self.logged = True

            if n == 0:
                return pa.array([], type=pa.large_binary())

            class _Frames(Dataset):
                def __init__(self, arr):
                    self.arr = arr

                def __len__(self):
                    return len(self.arr)

                def __getitem__(self, i):
                    # `frame` can be null (chunker emits a row even when no
                    # start frame decoded); null/undecodable -> None -> null out.
                    scalar = self.arr[i]
                    if not scalar.is_valid:
                        return None
                    try:
                        raw = scalar.as_buffer().to_pybytes()
                        return Image.open(io.BytesIO(raw)).convert("RGB")
                    except Exception:  # noqa: BLE001
                        return None

            loader_kwargs = dict(
                batch_size=self.batch_size,
                num_workers=self.num_workers,
                collate_fn=list,
            )
            if self.num_workers > 0:
                # Decode/prefetch upcoming frames while the (per-image, serial)
                # detector runs the current batch.
                loader_kwargs["prefetch_factor"] = 4
            loader = DataLoader(_Frames(col), **loader_kwargs)

            out: list[bytes | None] = []
            for images in loader:
                for img in images:
                    if img is None:
                        out.append(None)
                        continue
                    try:
                        pose = self.detector(
                            img,
                            include_hand=self.include_hand,
                            include_face=self.include_face,
                            output_type="pil",
                        )
                        buf = io.BytesIO()
                        pose.save(buf, format="PNG")
                        out.append(buf.getvalue())
                    except Exception as exc:  # noqa: BLE001
                        print("openpose_error", type(exc).__name__, flush=True)
                        out.append(None)

            return pa.array(out, type=pa.large_binary())

    return OpenPose()
