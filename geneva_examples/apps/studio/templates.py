"""Starter templates for UDF Studio, one per (kind, modality) starting point.

Each is runnable as-is under the Studio's contract (see :mod:`.runner`):
``transform(value)`` for UDFs, ``chunk(value)`` for chunkers. They mirror the
real factories in :mod:`geneva_examples.udfs` so a working prototype maps cleanly onto a
``@geneva.udf`` / ``@geneva.chunker`` when it's time to wire it into a stage.
"""

from __future__ import annotations

from textwrap import dedent

# name -> {kind, modality, code}
TEMPLATES: dict[str, dict] = {
    "image · dimensions (w×h)": {
        "kind": "udf",
        "modality": "image",
        "code": dedent(
            '''
            """UDF: decode an image and return its pixel dimensions."""

            def transform(image_bytes):
                from io import BytesIO
                from PIL import Image

                with Image.open(BytesIO(image_bytes)) as img:
                    width, height = img.size
                return {"width": int(width), "height": int(height)}
            '''
        ).strip(),
    },
    "image · file size (bytes)": {
        "kind": "udf",
        "modality": "image",
        "code": dedent(
            '''
            """UDF: byte length of the input — the simplest possible example."""

            def transform(value):
                return len(value)
            '''
        ).strip(),
    },
    "text · word + char count": {
        "kind": "udf",
        "modality": "text",
        "code": dedent(
            '''
            """UDF: basic text features from a string cell."""

            def transform(text):
                return {"chars": len(text), "words": len(text.split())}
            '''
        ).strip(),
    },
    "image · CLIP embedding": {
        "kind": "udf",
        "modality": "image",
        "code": dedent(
            '''
            """UDF: OpenCLIP image embedding.

            Module-level code runs ONCE when you hit Run, so the model loads a
            single time and `transform` reuses it across samples. The first run
            downloads weights and may take a while.
            """
            import open_clip
            import torch
            from io import BytesIO
            from PIL import Image

            model, _, preprocess = open_clip.create_model_and_transforms(
                "ViT-B-32", pretrained="laion2b_s34b_b79k"
            )
            model = model.eval()

            def transform(image_bytes):
                img = Image.open(BytesIO(image_bytes)).convert("RGB")
                tensor = preprocess(img).unsqueeze(0)
                with torch.no_grad():
                    feats = model.encode_image(tensor)
                    feats = feats / feats.norm(dim=-1, keepdim=True)
                return feats[0].tolist()
            '''
        ).strip(),
    },
    "video · fixed-length chunker": {
        "kind": "chunker",
        "modality": "video",
        "code": dedent(
            '''
            """Chunker: split a video into fixed-length windows.

            Yields one row per window. This prototype emits window bounds; the
            production chunker (geneva_examples/udfs/chunkers.py) also re-encodes the clip
            and extracts a start frame.
            """
            CHUNK_SECONDS = 10.0

            def chunk(video_bytes):
                import io
                import av

                with av.open(io.BytesIO(video_bytes)) as container:
                    stream = container.streams.video[0]
                    if stream.duration and stream.time_base:
                        duration = float(stream.duration * stream.time_base)
                    else:
                        duration = float(container.duration or 0) / 1_000_000.0

                start, chunk_id = 0.0, 0
                while start < duration:
                    end = min(start + CHUNK_SECONDS, duration)
                    yield {
                        "chunk_id": chunk_id,
                        "start_sec": round(start, 3),
                        "end_sec": round(end, 3),
                    }
                    start += CHUNK_SECONDS
                    chunk_id += 1
            '''
        ).strip(),
    },
    "audio · duration (seconds)": {
        "kind": "udf",
        "modality": "audio",
        "code": dedent(
            '''
            """UDF: decode an audio file and return its duration in seconds."""

            def transform(audio_bytes):
                import io
                import av

                with av.open(io.BytesIO(audio_bytes)) as container:
                    stream = container.streams.audio[0]
                    if stream.duration and stream.time_base:
                        return float(stream.duration * stream.time_base)
                    return float(container.duration or 0) / 1_000_000.0
            '''
        ).strip(),
    },
}

DEFAULT_TEMPLATE = "image · dimensions (w×h)"
