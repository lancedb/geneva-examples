"""Sentence-Transformers text-embedding UDF — the view's computed column.

A **batch** UDF (``__call__`` takes the whole ``pa.Array`` for a task, not one
row) that encodes a text column into a fixed-width ``list<float32>`` with
``sentence-transformers``. The default checkpoint — ``all-MiniLM-L6-v2``, 384
dims, ~22M params — auto-downloads to the HF cache on first use, so nothing has
to be pre-placed and the whole example runs offline afterwards, CPU included.

Batch-at-a-time is the point: the model is loaded **once** per actor in
``setup()`` and each call encodes ``len(col)`` descriptions in
``batch_size``-sized forward passes. A per-row UDF would pay Python-call and
tokenization overhead per description and leave the GPU idle between rows.

Unlike the other feature stages in this repo, this UDF is not backfilled into its
source table — it is handed to ``select({"embedding": EmbedDescription()})`` and
computed by a **materialized view** on ``refresh``. Nothing about the UDF changes
for that; the factory returns the same decorated instance either way.

Embeddings are L2-normalized, so a KNN search over the column ranks by cosine
similarity.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from typing import Any

from geneva_examples.core.package_specs import package_spec

# Small, fast, and good enough to make semantic search visibly work on CPU.
DEFAULT_MODEL = os.environ.get(
    "SENTENCE_TRANSFORMERS_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
)

# Output width of the checkpoints this example is likely to be pointed at, so
# `--model-name` alone is enough (see `resolve_dim`). Anything else needs an
# explicit `--dim`; whatever is resolved is verified against the loaded model in
# `setup()`, so a wrong value fails with a clear message instead of writing
# malformed vectors.
MODEL_DIMS: dict[str, int] = {
    "sentence-transformers/all-MiniLM-L6-v2": 384,
    "sentence-transformers/all-MiniLM-L12-v2": 384,
    "sentence-transformers/paraphrase-MiniLM-L6-v2": 384,
    "sentence-transformers/all-mpnet-base-v2": 768,
    "sentence-transformers/multi-qa-mpnet-base-dot-v1": 768,
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
    "intfloat/e5-small-v2": 384,
    "intfloat/e5-base-v2": 768,
}

# Geneva remote runtime package pins (env-overridable for targeting other builds).
# geneva/lancedb/pylance/sentence-transformers track the installed versions so the
# workers embed with the same model code the client resolved; the rest stay
# exact-pinned for reproducible worker builds.
GENEVA_PACKAGE_SPEC = package_spec("geneva")
LANCEDB_PACKAGE_SPEC = package_spec("lancedb")
PYLANCE_PACKAGE_SPEC = package_spec("pylance")
SENTENCE_TRANSFORMERS_PACKAGE_SPEC = package_spec("sentence-transformers")
PYARROW_PACKAGE_SPEC = os.environ.get("PYARROW_PACKAGE_SPEC", "pyarrow==23.0.1")
NUMPY_PACKAGE_SPEC = os.environ.get("NUMPY_PACKAGE_SPEC", "numpy==2.4.6")
TORCH_PACKAGE_SPEC = os.environ.get("TORCH_PACKAGE_SPEC", "torch==2.12.0")

# sentence-transformers pulls transformers/tokenizers/scikit-learn itself; only
# the pins we deliberately control are listed.
SENTENCE_TRANSFORMERS_RUNTIME_PIP = [
    GENEVA_PACKAGE_SPEC,
    LANCEDB_PACKAGE_SPEC,
    PYLANCE_PACKAGE_SPEC,
    PYARROW_PACKAGE_SPEC,
    NUMPY_PACKAGE_SPEC,
    TORCH_PACKAGE_SPEC,
    SENTENCE_TRANSFORMERS_PACKAGE_SPEC,
]


def resolve_dim(model_name: str, dim: int = 0) -> int:
    """Embedding width for ``model_name``, or an explicit ``dim`` if positive.

    A fixed-size list column has to declare its width when the view is created,
    before any worker has loaded the model — so the driver resolves it here from
    the known-checkpoint table and asks for ``--dim`` when the name is unknown.
    """
    if dim > 0:
        return dim
    known = MODEL_DIMS.get(model_name)
    if known is None:
        raise ValueError(
            f"unknown embedding width for model {model_name!r}: pass --dim "
            "explicitly (the view's vector column declares its width up front). "
            f"Known models: {', '.join(sorted(MODEL_DIMS))}"
        )
    return known


def build_embed_description_udf(
    *,
    input_column: str = "description",
    manifest: Any,
    model_name: str = DEFAULT_MODEL,
    dim: int = 0,
    batch_size: int = 64,
    device: str = "",
    num_cpus: float = 2.0,
    num_gpus: float | None = None,
    memory_bytes: int = 4 * 1024**3,
    checkpoint_size: int = 512,
    task_size: int = 512,
):
    """Build the batch text-embedding UDF that reads ``input_column``.

    Returns the decorated instance, ready to drop into a view's projection::

        table.search(None).select({..., "embedding": EmbedDescription()})

    ``dim`` defaults to ``resolve_dim(model_name)``; ``device`` empty lets
    sentence-transformers pick (CUDA/MPS/CPU).
    """
    import geneva
    import pyarrow as pa

    _model_name = model_name
    _dim = resolve_dim(model_name, dim)
    _batch_size = max(1, int(batch_size))
    _device = device or None

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
        # Records what produced the column, so a view's vectors can be traced
        # back to a checkpoint without re-reading this file.
        field_metadata={"model": _model_name, "normalized": "l2"},
    )
    class EmbedDescription(Callable):
        def __init__(self):
            self.is_loaded = False
            self.logged = False
            self.model_name = _model_name
            self.dim = _dim
            self.batch_size = _batch_size
            self.device = _device

        def setup(self):
            # RUNS ON THE WORKER (local Ray or the remote Geneva runtime), once
            # per actor. Every import stays nested: this module is not importable
            # there, only the manifest's pip packages are.
            from sentence_transformers import SentenceTransformer

            self.model = SentenceTransformer(self.model_name, device=self.device)
            # `get_sentence_embedding_dimension` was renamed to
            # `get_embedding_dimension`; accept either so the worker's pinned
            # version doesn't have to match the client's spelling.
            reader = getattr(self.model, "get_embedding_dimension", None)
            reported = (
                reader() if reader else self.model.get_sentence_embedding_dimension()
            )
            # The view's column width was fixed at creation time; a mismatch here
            # would otherwise surface as an opaque Arrow error mid-refresh.
            if reported is not None and int(reported) != self.dim:
                raise ValueError(
                    f"model {self.model_name!r} emits {reported}-dim embeddings "
                    f"but the column declares {self.dim}: recreate the view with "
                    f"--dim {reported}"
                )
            self.is_loaded = True

        def __call__(self, col: pa.Array) -> pa.Array:
            import numpy as np
            import pyarrow as pa

            if not self.is_loaded:
                self.setup()

            vector_type = pa.list_(pa.float32(), self.dim)
            texts = col.to_pylist()
            n = len(texts)
            if n == 0:
                return pa.array([], type=vector_type)

            if not self.logged:
                # print -> the worker's stdout (driver logs in local mode).
                print(
                    "embed_description_udf",
                    {
                        "model": self.model_name,
                        "device": str(getattr(self.model, "device", "")),
                        "rows": n,
                        "batch_size": self.batch_size,
                        "dim": self.dim,
                    },
                    flush=True,
                )
                self.logged = True

            # A null/blank description has no meaningful embedding: encode only
            # the rows that have text and leave the rest null, rather than
            # embedding the empty string and pretending it's a product.
            valid = [i for i, text in enumerate(texts) if text and text.strip()]
            if not valid:
                return pa.array([None] * n, type=vector_type)

            vectors = self.model.encode(
                [texts[i] for i in valid],
                batch_size=self.batch_size,
                # Unit vectors: a KNN search then ranks by cosine similarity.
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
            vectors = np.asarray(vectors, dtype=np.float32)

            if len(valid) == n:
                return pa.FixedSizeListArray.from_arrays(
                    pa.array(vectors.reshape(-1), type=pa.float32()), self.dim
                )
            # Scatter the encoded rows back into a full-length column of nulls.
            rows: list[Any] = [None] * n
            for position, index in enumerate(valid):
                rows[index] = vectors[position].tolist()
            return pa.array(rows, type=vector_type)

    return EmbedDescription()


def encode_query(query: str, *, model_name: str = DEFAULT_MODEL) -> list[float]:
    """Encode one query string on the **driver**, for the search demo.

    Same checkpoint and same L2 normalization as the UDF, so the resulting vector
    is comparable to the view's ``embedding`` column. Imported lazily because the
    driver does not otherwise need the model — the refresh runs it on the workers.
    """
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(model_name)
    vector = model.encode(
        [query],
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )[0]
    return [float(x) for x in vector]
