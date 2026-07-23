"""Generated console-script entry points, one per example step.

Each name below is a ``click.Command`` built from the step's spec, so the
``uv run <name>`` commands, their ``--help``, and their parameters all come from
the single source of truth in each example's package. Referenced by
``[project.scripts]`` in ``pyproject.toml``.
"""

from __future__ import annotations

from geneva_examples.core.spec import build_command
from geneva_examples.examples import audio, debugging, images, pdf, video

# --- images -----------------------------------------------------------------
ingest_images = build_command(images.EXAMPLE, images.INGEST)
lightweight = build_command(images.EXAMPLE, images.LIGHTWEIGHT)
embed = build_command(images.EXAMPLE, images.EMBED)
caption = build_command(images.EXAMPLE, images.CAPTION)

# --- video ------------------------------------------------------------------
ingest_videos = build_command(video.EXAMPLE, video.INGEST)
ingest_videos_openvid = build_command(video.EXAMPLE, video.INGEST_OPENVID)
ingest_videos_external = build_command(video.EXAMPLE, video.INGEST_EXTERNAL)
chunk_videos = build_command(video.EXAMPLE, video.CHUNK)
chunk_videos_openvid = build_command(video.EXAMPLE, video.CHUNK_OPENVID)
chunk_videos_external = build_command(video.EXAMPLE, video.CHUNK_EXTERNAL)
frame_embed = build_command(video.EXAMPLE, video.FRAME_EMBED)
frame_caption = build_command(video.EXAMPLE, video.FRAME_CAPTION)
frame_openpose = build_command(video.EXAMPLE, video.FRAME_OPENPOSE)
seed_video_clips = build_command(video.EXAMPLE, video.SEED)

# --- pdf --------------------------------------------------------------------
ingest_pdfs = build_command(pdf.EXAMPLE, pdf.INGEST)
chunk_pdfs = build_command(pdf.EXAMPLE, pdf.CHUNK)

# --- audio ------------------------------------------------------------------
ingest_audio = build_command(audio.EXAMPLE, audio.INGEST)
synthesize_audio = build_command(audio.EXAMPLE, audio.SYNTHESIZE)
transcribe_audio = build_command(audio.EXAMPLE, audio.TRANSCRIBE)
export_audio = build_command(audio.EXAMPLE, audio.EXPORT)

# --- debugging demo ---------------------------------------------------------
demo_errors = build_command(debugging.EXAMPLE, debugging.DEMO_ERRORS)
