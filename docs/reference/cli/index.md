<!-- GENERATED FILE — do not edit. `make docs` regenerates it. -->
# Command index

> **Generated file — do not edit.** Rendered from the spec registry by
> `geneva_examples/docs_gen/`; regenerate with `make docs`.
> Docs index: [`docs/README.md`](../../README.md).

All 21 pipeline commands are generated from the spec registry
(`geneva_examples/examples/__init__.py`); run any of them as
`uv run <command>`. Grep hint: every command has an H2 heading equal
to its name in the per-example pages, e.g.
`grep -n "^## \`chunk-videos\`" docs/reference/cli/video.md`.

## Pipeline commands

| Command | Example | What it does | GPU | Prerequisite |
|---|---|---|---|---|
| [`ingest-images`](images.md#ingest-images) | images | Ingest images | no | — |
| [`lightweight`](images.md#lightweight) | images | File size + dimensions | no | run ingest-images first |
| [`embed`](images.md#embed) | images | OpenCLIP embeddings | yes | run ingest-images first |
| [`caption`](images.md#caption) | images | BLIP captions | yes | run ingest-images first |
| [`ingest-videos`](video.md#ingest-videos) | video | Ingest videos | no | — |
| [`ingest-videos-openvid`](video.md#ingest-videos-openvid) | video | Ingest videos (OpenVid, reference-only) | no | — |
| [`ingest-videos-external`](video.md#ingest-videos-external) | video | Ingest videos (external refs, reference-only) | no | — |
| [`chunk-videos`](video.md#chunk-videos) | video | Chunk videos into clips | no | run ingest-videos first |
| [`chunk-videos-openvid`](video.md#chunk-videos-openvid) | video | Chunk videos (OpenVid blobs) | no | run ingest-videos-openvid first |
| [`chunk-videos-external`](video.md#chunk-videos-external) | video | Chunk videos (external refs) | no | run ingest-videos-external first |
| [`frame-embed`](video.md#frame-embed) | video | OpenCLIP embeddings on frames | yes | run a chunk step first |
| [`frame-caption`](video.md#frame-caption) | video | BLIP captions on frames | yes | run a chunk step first |
| [`frame-openpose`](video.md#frame-openpose) | video | OpenPose skeletons on frames | yes | run a chunk step first |
| [`seed-video-clips`](video.md#seed-video-clips) | video | Seed clips (load-test helper) | no | run ingest-videos-openvid first (or pass --seed-clip-table) |
| [`ingest-pdfs`](pdf.md#ingest-pdfs) | pdf | Ingest PDFs | no | — |
| [`chunk-pdfs`](pdf.md#chunk-pdfs) | pdf | Extract pages + chunks | no | run ingest-pdfs first |
| [`ingest-audio`](audio.md#ingest-audio) | audio | Ingest text prompts | no | — |
| [`synthesize-audio`](audio.md#synthesize-audio) | audio | MMS-TTS speech synthesis | yes | run ingest-audio first |
| [`transcribe-audio`](audio.md#transcribe-audio) | audio | Whisper transcription | yes | run synthesize-audio first |
| [`export-audio`](audio.md#export-audio) | audio | Export waveforms to .wav | no | run synthesize-audio first |
| [`demo-errors`](debugging.md#demo-errors) | debugging | Generate debuggable errors | no | — |

## Operator tools (not spec-generated)

| Command | What it does | Entrypoint |
|---|---|---|
| `tui` | Interactive Textual runner: Tables / Jobs / Examples over the same specs | `geneva_examples/tui/app.py` |
| `stats` | Table row counts, schema, feature-column population (defaults: `images`, `videos`, `video_clips`; pass `--table` for `pdfs`/`audio`/`debug_demo`) | `geneva_examples/ops/stats.py` |
| `jobs` | List, show, tail, and kill Geneva backfill job records | `geneva_examples/ops/jobs.py` |
| `cleanup` | Drop `videos`/`video_clips`, optionally a pdfs table | `geneva_examples/ops/cleanup.py` |
| `udf-studio` | Gradio UDF prototyping sandbox (runs editor code unsandboxed — loopback only) | `geneva_examples/apps/udf_studio.py` |
