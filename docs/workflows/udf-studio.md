# UDF Studio

> Part of the geneva-examples docs — index: [docs/README.md](../README.md).

## Contents

- [Launching the Studio](#launching-the-studio)
- [Security posture](#security-posture)
- [The execution contract](#the-execution-contract)
- [Sample data](#sample-data)
- [Templates](#templates)
- [The UDF library](#the-udf-library)
- [Promoting a prototype](#promoting-a-prototype)

## Launching the Studio

UDF Studio is a Gradio app for prototyping a Geneva UDF (`transform(value)`) or
chunker (`chunk(value)`) against files on your own disk, entirely in the driver
process — no Ray, no GPU, no cluster, no manifest. It exists to make the edit → run
→ inspect loop fast before you wire a finished function into a real step.

Run `uv run udf-studio` and open http://127.0.0.1:7860. It is an operator tool, not a
spec-generated command, so its flags live here rather than in the generated CLI
reference (see [docs/reference/cli/index.md](../reference/cli/index.md)); source:
`geneva_examples/apps/udf_studio.py`.

| Flag | Default | Effect |
|---|---|---|
| `--host` | `127.0.0.1` | Bind address for the Gradio server |
| `--port` | `7860` | Server port |
| `--data-dir` | `studio_data` | Directory holding the per-modality sample data |
| `--library` | `udf_library` | Local LanceDB path for saved functions |
| `--share` | off | Create a public Gradio share link (see Security posture) |
| `--log-level` | `INFO` | Logging level |

## Security posture

The Studio executes whatever is in the editor **in this process with no sandbox** —
`exec` on the driver, by design (the `ruff` S102 exemption for
`geneva_examples/apps/studio/runner.py` in `pyproject.toml` acknowledges this
explicitly). Anyone who can reach the server can run arbitrary Python on the machine
hosting it.

Hosts treated as private are the loopback allowlist `_LOOPBACK_HOSTS` in
`geneva_examples/apps/udf_studio.py`: `127.0.0.1`, `localhost`, `::1`, and
`0:0:0:0:0:0:0:1`. Binding any other host, or passing `--share`, logs a `SECURITY:`
WARNING — **but the server still starts**. There is no refusal path; the warning is
the only guard. Treat a non-loopback bind or a share link as handing remote code
execution to everyone on that network, and only do it on a network you fully trust.

The Studio itself never connects to the cluster: it builds no Geneva manifest and
submits no jobs. That limits what the *app* does, not what an attacker can do —
arbitrary Python on the driver can read `config.yaml`, so an exposed Studio hands
over every credential on that machine (see [SECURITY.md](../../SECURITY.md)).

## The execution contract

The contract is deliberately small and decoupled from the `@geneva.udf` /
`@geneva.chunker` decorators; `geneva_examples/apps/studio/runner.py` is the
authoritative source.

- **UDF mode** — the code must define `transform(value)`: one input element in, one
  output out, mapped over the loaded samples.
- **Chunker mode** — the code must define `chunk(value)`: a generator yielding one
  `dict` per output row. A non-dict yield is shown under a single `value` column.
- **Module-level code runs once per Run, in a fresh namespace.** Load models at
  module level so `transform` reuses them across samples; no state carries over
  between Runs.
- **Optional `setup()`** — if the code defines one, it is called once immediately
  after the module-level code executes.
- **Errors are captured, never raised.** A failure while compiling the code or in
  `setup()` puts the full traceback in the error box; a per-row exception becomes an
  `error` cell for that row and the run continues, so one bad sample never sinks the
  run. With no samples loaded, Run reports "Load some samples first."

Each `value` is whatever the sampler produced the last time you clicked
**Load samples** — switching the Input modality radio (or loading a template, which
overwrites it) does not reload or clear the samples, so reload after switching.
Types: raw `bytes` for image, video, audio, and pdf; a `str` (one CSV cell) for
text. Outputs
in the result table are display previews — bytes render as `<N bytes>` and long
lists are truncated — not the raw return values.

## Sample data

This is the canonical description of the sample-data layout (`studio_data/README.md`
points here). The Studio reads from `--data-dir` (default `studio_data/`), or from
whatever path you type into the **Data directory** field; one source per modality.
Resolution logic: `geneva_examples/apps/studio/samples.py`.

| Modality | Source | Each sample is | Recognized extensions |
|---|---|---|---|
| image | `images/` | the file's raw bytes | `.png` `.jpg` `.jpeg` `.gif` `.webp` `.bmp` `.tif` `.tiff` |
| video | `videos/` | the file's raw bytes | `.mp4` `.mov` `.mkv` `.avi` `.webm` `.m4v` |
| audio | `audio/` | the file's raw bytes | `.wav` `.mp3` `.flac` `.ogg` `.m4a` `.aac` |
| pdf | `pdfs/` | the file's raw bytes | `.pdf` |
| text | `input.csv` | one cell from the chosen column | n/a (CSV header drives the column dropdown) |

**Fallback rule**: if a modality's folder contains files but none carry a recognized
extension, the Studio serves **every** (non-hidden) file in the folder as that
modality — the assumption is that you know your own data. Files whose names start
with `.` are always skipped, listing is sorted by filename, and the **Samples**
field caps how many are loaded (UI default 4).

In the repo's own `studio_data/`, the four media subdirectories are tracked empty —
`.gitignore` excludes their contents and keeps only `.gitkeep` — so your media never
gets committed. `input.csv` ships with a few example text rows; replace it with your
own.

## Templates

Eight starter templates ship in `geneva_examples/apps/studio/templates.py`, each
runnable as-is against the matching sample data. They mirror the real factories
under `geneva_examples/examples/` so a working prototype maps cleanly onto a
production UDF or chunker.

| Template | Kind | Modality | What it does |
|---|---|---|---|
| image · dimensions (w×h) | udf | image | PIL decode → width/height (the default template) |
| image · file size (bytes) | udf | image | `len(value)` — the simplest possible example |
| text · word + char count | udf | text | character and word counts of a CSV cell |
| image · CLIP embedding | udf | image | module-level OpenCLIP ViT-B-32; the first Run downloads weights |
| video · fixed-length chunker | chunker | video | PyAV; yields 10-second window bounds per clip |
| audio · duration (seconds) | udf | audio | PyAV; decoded duration in seconds |
| pdf · page + word count | udf | pdf | pypdf; page and word counts |
| pdf · text chunker | chunker | pdf | pypdf + RecursiveCharacterTextSplitter (2048/200); one row per chunk |

Clicking **Load template** replaces the editor contents **and overwrites the
Function kind and Input modality radios** with the template's values — switch them
back if you were adapting a template across kinds. The dropdown lists all eight
templates regardless of the currently selected kind and modality, and some
combinations ship no template (a video UDF; an image, audio, or text chunker):
start from
the nearest template and adapt it.

## The UDF library

Work in progress persists to a plain on-disk LanceDB at the `--library` path
(default `udf_library`), or at whatever path you type into the **Library path**
field — a personal scratch library, entirely separate from the database the
pipelines talk to. Source: `geneva_examples/apps/studio/library.py`.

One table, `udfs`, keyed by `name`:

| Column | Contents |
|---|---|
| `name` | the save name; the key. Saving an existing name **overwrites** it |
| `kind` | `udf` or `chunker` |
| `modality` | the selected input modality |
| `code` | the full editor contents |
| `updated_at` | ISO-8601 timestamp (seconds precision) set at save time |

Saving is an atomic upsert-by-name (`merge_insert` — a crash cannot leave the row
deleted but not re-added); a blank name is rejected. The **Saved** dropdown lists
functions newest-first by `updated_at`, and loading one restores the code plus its
kind and modality radios. An unreadable library path never crashes launch — the
Studio logs a warning and shows an empty list.

## Promoting a prototype

The Studio's contract is deliberately decoupled from Geneva's decorators, so
promotion is a manual step: wrap the working function in a `@geneva.udf` /
`@geneva.chunker` factory, add a `run(cfg, *, ...)` module and a `Step` to the
example package under `geneva_examples/examples/<pkg>/`, and register it. The full
checklist is [docs/authoring/adding-a-step.md](../authoring/adding-a-step.md).

One behavioral difference matters when porting: the Studio runs on the driver, where
module-level imports work, but a real worker UDF must nest all imports and helpers
inside the decorated function body — see the closure rule in
[docs/authoring/writing-udfs.md](../authoring/writing-udfs.md).
