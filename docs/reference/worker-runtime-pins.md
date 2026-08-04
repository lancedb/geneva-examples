<!-- GENERATED FILE — do not edit. `make docs` regenerates it. -->
# Worker runtime pins and `*_PACKAGE_SPEC` overrides

> **Generated file — do not edit.** Values are read from the installed
> environment (`uv.lock`) and the manifest modules; regenerate with
> `make docs` after any pin change.
> Docs index: [`docs/README.md`](../README.md).

Each remote-worker manifest installs the pip specs below (enterprise
mode only; local mode installs nothing). `geneva`, `lancedb`, and
`pylance` track the client's installed versions via
`geneva_examples/core/package_specs.py` (`package_spec`); the rest are
pinned in the module named in each row. Every spec can be overridden
verbatim by its environment variable.

## Manifests

| Manifest | Module | Pip specs |
|---|---|---|
| `IMAGEINFO_RUNTIME_PIP` | `geneva_examples/examples/images/imageinfo.py` | `geneva==0.14.1b5`, `lancedb==0.35.0b2`, `pylance==9.1.0b2`, `pyarrow==23.0.1`, `pillow==12.2.0` |
| `CLIP_RUNTIME_PIP` | `geneva_examples/examples/_shared/clip.py` | `geneva==0.14.1b5`, `lancedb==0.35.0b2`, `pylance==9.1.0b2`, `pyarrow==23.0.1`, `pillow==12.2.0`, `numpy==2.4.6`, `torch==2.12.0`, `open-clip-torch==3.3.0` |
| `BLIP_RUNTIME_PIP` | `geneva_examples/examples/_shared/blip.py` | `geneva==0.14.1b5`, `lancedb==0.35.0b2`, `pylance==9.1.0b2`, `pyarrow==23.0.1`, `pillow==12.2.0`, `torch==2.12.0`, `transformers==5.9.0` |
| `VIDEO_RUNTIME_PIP` | `geneva_examples/examples/video/chunkers.py` | `geneva==0.14.1b5`, `lancedb==0.35.0b2`, `pylance==9.1.0b2`, `pyarrow==23.0.1`, `pillow==12.2.0`, `av>=12,<14` |
| `OPENPOSE_RUNTIME_PIP` | `geneva_examples/examples/video/openpose.py` | `geneva==0.14.1b5`, `lancedb==0.35.0b2`, `pylance==9.1.0b2`, `pyarrow==23.0.1`, `pillow==12.2.0`, `numpy==2.4.6`, `torch==2.12.0`, `controlnet-aux>=0.0.7` |
| `BASE_RUNTIME_PIP` | `geneva_examples/examples/video/seed.py` | `geneva==0.14.1b5`, `lancedb==0.35.0b2`, `pylance==9.1.0b2`, `pyarrow==23.0.1` |
| `PDF_RUNTIME_PIP` | `geneva_examples/examples/pdf/document.py` | `geneva==0.14.1b5`, `lancedb==0.35.0b2`, `pylance==9.1.0b2`, `pyarrow==23.0.1`, `pypdf>=5,<6`, `langchain-text-splitters>=0.3,<0.4` |
| `MMS_TTS_RUNTIME_PIP` | `geneva_examples/examples/audio/tts.py` | `geneva==0.14.1b5`, `lancedb==0.35.0b2`, `pylance==9.1.0b2`, `pyarrow==23.0.1`, `numpy==2.4.6`, `torch==2.12.0`, `transformers==5.0.0` |
| `WHISPER_RUNTIME_PIP` | `geneva_examples/examples/audio/transcribe.py` | `geneva==0.14.1b5`, `lancedb==0.35.0b2`, `pylance==9.1.0b2`, `pyarrow==23.0.1`, `numpy==2.4.6`, `torch==2.12.0`, `transformers==5.0.0` |
| `FAULTY_RUNTIME_PIP` | `geneva_examples/examples/debugging/faulty.py` | *(empty — geneva builtins only)* |

## Override environment variables

Setting one of these before a stage command replaces the spec verbatim
in every manifest built afterwards (it need not be an `==` pin).

| Env var | Current value | Defined in |
|---|---|---|
| `PYARROW_PACKAGE_SPEC` | `pyarrow==23.0.1` | `geneva_examples/examples/images/imageinfo.py`, `geneva_examples/examples/_shared/clip.py`, `geneva_examples/examples/_shared/blip.py`, `geneva_examples/examples/video/chunkers.py`, `geneva_examples/examples/video/openpose.py`, `geneva_examples/examples/pdf/document.py`, `geneva_examples/examples/audio/tts.py`, `geneva_examples/examples/audio/transcribe.py` |
| `PILLOW_PACKAGE_SPEC` | `pillow==12.2.0` | `geneva_examples/examples/images/imageinfo.py`, `geneva_examples/examples/_shared/clip.py`, `geneva_examples/examples/_shared/blip.py`, `geneva_examples/examples/video/chunkers.py`, `geneva_examples/examples/video/openpose.py` |
| `GENEVA_PACKAGE_SPEC` | `geneva==0.14.1b5` | `geneva_examples/examples/images/imageinfo.py`, `geneva_examples/examples/_shared/clip.py`, `geneva_examples/examples/_shared/blip.py`, `geneva_examples/examples/video/chunkers.py`, `geneva_examples/examples/video/openpose.py`, `geneva_examples/examples/pdf/document.py`, `geneva_examples/examples/audio/tts.py`, `geneva_examples/examples/audio/transcribe.py` |
| `LANCEDB_PACKAGE_SPEC` | `lancedb==0.35.0b2` | `geneva_examples/examples/images/imageinfo.py`, `geneva_examples/examples/_shared/clip.py`, `geneva_examples/examples/_shared/blip.py`, `geneva_examples/examples/video/chunkers.py`, `geneva_examples/examples/video/openpose.py`, `geneva_examples/examples/pdf/document.py`, `geneva_examples/examples/audio/tts.py`, `geneva_examples/examples/audio/transcribe.py` |
| `PYLANCE_PACKAGE_SPEC` | `pylance==9.1.0b2` | `geneva_examples/examples/images/imageinfo.py`, `geneva_examples/examples/_shared/clip.py`, `geneva_examples/examples/_shared/blip.py`, `geneva_examples/examples/video/chunkers.py`, `geneva_examples/examples/video/openpose.py`, `geneva_examples/examples/pdf/document.py`, `geneva_examples/examples/audio/tts.py`, `geneva_examples/examples/audio/transcribe.py` |
| `NUMPY_PACKAGE_SPEC` | `numpy==2.4.6` | `geneva_examples/examples/_shared/clip.py`, `geneva_examples/examples/video/openpose.py`, `geneva_examples/examples/audio/tts.py`, `geneva_examples/examples/audio/transcribe.py` |
| `TORCH_PACKAGE_SPEC` | `torch==2.12.0` | `geneva_examples/examples/_shared/clip.py`, `geneva_examples/examples/_shared/blip.py`, `geneva_examples/examples/video/openpose.py`, `geneva_examples/examples/audio/tts.py`, `geneva_examples/examples/audio/transcribe.py` |
| `OPEN_CLIP_PACKAGE_SPEC` | `open-clip-torch==3.3.0` | `geneva_examples/examples/_shared/clip.py` |
| `TRANSFORMERS_PACKAGE_SPEC` | `transformers==5.9.0` | `geneva_examples/examples/_shared/blip.py` |
| `TRANSFORMERS_PACKAGE_SPEC` | `transformers==5.0.0` | `geneva_examples/examples/audio/tts.py`, `geneva_examples/examples/audio/transcribe.py` |
| `AV_PACKAGE_SPEC` | `av>=12,<14` | `geneva_examples/examples/video/chunkers.py` |
| `CONTROLNET_AUX_PACKAGE_SPEC` | `controlnet-aux>=0.0.7` | `geneva_examples/examples/video/openpose.py` |
| `PYPDF_PACKAGE_SPEC` | `pypdf>=5,<6` | `geneva_examples/examples/pdf/document.py` |
| `LANGCHAIN_TEXT_SPLITTERS_PACKAGE_SPEC` | `langchain-text-splitters>=0.3,<0.4` | `geneva_examples/examples/pdf/document.py` |

Note: `TRANSFORMERS_PACKAGE_SPEC` resolves to *different* defaults per module (one row
per value above) — an override replaces every occurrence.

## Deviations from the naming convention

The convention (`geneva_examples/core/package_specs.py`,
`_default_env_var`) is `{DISTRIBUTION}_PACKAGE_SPEC` with
non-alphanumerics collapsed to `_`. Deviations:

| Package | Conventional name | Actual env var read | Where |
|---|---|---|---|
| `open-clip-torch` | `OPEN_CLIP_TORCH_PACKAGE_SPEC` | `OPEN_CLIP_PACKAGE_SPEC` | `geneva_examples/examples/_shared/clip.py` |
