# Audio workflow: the text → speech → text round trip

> Part of the geneva-examples docs — index: [docs/README.md](../README.md).

The audio example seeds a table of text prompts, synthesizes speech waveforms from
them with MMS-TTS, transcribes the waveforms back to text with Whisper, and finally
writes the audio out as WAV files. Both models auto-download on first use, so the
pipeline needs no external media, no pre-placed model files, and no cluster —
only network access on the first run of each model step, after which it runs
fully offline in local mode.

## Contents

- [The round trip](#the-round-trip)
- [Run it](#run-it)
- [Prompts and models](#prompts-and-models)
- [Re-running](#re-running)
- [Export](#export)
- [Cleanup caveat](#cleanup-caveat)

## The round trip

Four steps operate on one table (default name `audio`). The first three build the
text → speech → text loop; the fourth turns the stored waveforms into files you can
listen to.

| Step | Command | Output | GPU step |
|---|---|---|---|
| ingest | `uv run ingest-audio` | creates the `audio` table: `id` (`string`), `text` (`string`) | no |
| synthesize | `uv run synthesize-audio` | backfills the `audio` waveform column, `list<float32>` at 16 kHz | yes |
| transcribe | `uv run transcribe-audio` | backfills the `transcript` column (`large_string`) | yes |
| export | `uv run export-audio` | writes `<out-dir>/<id>.wav` files on the driver; adds no column | no |

The two GPU steps run CPU-only in local mode, where GPU requests are forced to
zero — see [docs/reference/local-mode.md](../reference/local-mode.md). Column
details are in [docs/reference/tables-and-schemas.md](../reference/tables-and-schemas.md).

## Run it

```bash
uv run ingest-audio
uv run synthesize-audio
uv run transcribe-audio
uv run export-audio
```

Each step ends with a greppable success sentinel:

| Step | Final log line |
|---|---|
| `ingest-audio` | `ingest_audio_ok` |
| `synthesize-audio` | `synthesize_audio_ok` |
| `transcribe-audio` | `transcribe_audio_ok` (after a `transcript_sample` table) |
| `export-audio` | `export_audio_ok wrote <n> skipped <m> dir <path>` |

The two backfill steps also log the shared backfill lines (`job <column> <job_id>`,
`backfill_seconds`, `null_<column> <count>`) from `geneva_examples/core/backfill.py`.
First runs download model weights into `./huggingface_cache` — the model steps set
`HF_HOME` there (`geneva_examples/examples/audio/synthesize.py`,
`geneva_examples/examples/audio/transcribe.py`). Full per-command flag tables live
in the generated reference: [docs/reference/cli/audio.md](../reference/cli/audio.md).

## Prompts and models

**Prompts.** `ingest-audio` seeds five hardcoded prompts (ids `greeting`, `pangram`,
`weather`, `numbers`, `cantina`), each deliberately short — well under Whisper's
30-second transcription window — so the round trip transcribes cleanly and runs
fast on CPU. See `PROMPTS` in `geneva_examples/examples/audio/ingest.py` for the
exact texts.

**Synthesis model.** The synthesize step's UDF loads Meta's MMS-TTS English
checkpoint `facebook/mms-tts-eng`, a VITS model that `transformers` loads directly
via `VitsModel`/`VitsTokenizer` (`geneva_examples/examples/audio/tts.py`). The only
override is the `MMS_TTS_MODEL_ID` environment variable — there is no CLI flag for
it (`tts.py:32`; see
[docs/reference/environment-variables.md](../reference/environment-variables.md)).
The UDF's `setup()` checks the loaded model's `config.sampling_rate` and raises a
`RuntimeError` if it is not 16000 Hz, because the downstream Whisper stage assumes
16 kHz input — a substitute model set via `MMS_TTS_MODEL_ID` must emit 16 kHz
(`tts.py:107-115`).

**Waveform format.** The output is a variable-length `list<float32>` waveform at
16 kHz; the column's field metadata records the rate as `sample_rate_hz`
(`tts.py:88`), so downstream consumers can read it rather than assume it.

**Transcription model.** The transcribe step writes no new UDF body: it reuses
geneva's shipped `WhisperChunkTranscriber` from `geneva.udfs.audio`, rebinding its
input column from the shipped default `samples` to `audio` and attaching this
repo's manifest via `attrs.evolve` (`geneva_examples/examples/audio/transcribe.py`).
The checkpoint defaults to `openai/whisper-large-v3-turbo` (`transcribe.py:99`;
flag table in [docs/reference/cli/audio.md#transcribe-audio](../reference/cli/audio.md#transcribe-audio)),
and any `transformers` sequence-to-sequence ASR checkpoint (the Whisper family,
e.g. `openai/whisper-tiny`) works via `--model-id` — geneva's transcriber loads
the checkpoint through `AutoModelForSpeechSeq2Seq`, so CTC checkpoints such as
wav2vec2 do not work. The TTS output is
already `list<float32>` at 16 kHz — exactly what the transcriber expects — so the
two stages chain with no resampling.

## Re-running

`synthesize-audio` and `transcribe-audio` are incremental by default: each run
fills only the rows whose output column is still NULL, reusing the column's
already-registered UDF, so a partial or interrupted run can simply be run again.
Pass `--reset` to drop the column and recompute every row — destructive, but the
only way to make a model change take effect: an incremental run keeps the
originally registered UDF binding, so a new `MMS_TTS_MODEL_ID` or `--model-id`
does nothing without `--reset` (`geneva_examples/core/backfill.py:113-118`). The
full contract is in [docs/concepts/backfills.md](../concepts/backfills.md).

Re-running the bare `uv run ingest-audio` drops and recreates the whole table,
which discards the synthesized and transcribed columns along with it — a full
pipeline restart. There is no non-destructive re-ingest: `--no-overwrite` only
skips the drop, so `create_table` then fails on the existing table
(`geneva_examples/examples/audio/ingest.py:67-83`).

## Export

`export-audio` is the one step whose output is files, not a column. It runs
entirely on the driver — a plain table scan plus file writes — so it behaves
identically in local and enterprise modes; in enterprise mode the waveform values
are pulled back from the cluster (`geneva_examples/examples/audio/export.py`).

Each row becomes one mono 16-bit PCM WAV written with the stdlib `wave` module (no
extra audio dependency): samples are clipped to `[-1, 1]` and scaled to `int16`
(`export.py:26-38`). Files land in `--out-dir` (default `/tmp/geneva_audio`) named
`<id>.wav`, with ids sanitized to alphanumerics plus `-`, `_`, `.` — anything else
becomes `_`. Rows whose `audio` value is NULL (not yet synthesized) are skipped and
counted in the final `export_audio_ok wrote <n> skipped <m>` line. If the `audio`
column does not exist at all, the step fails fast with
`RuntimeError: table 'audio' has no 'audio' column — run the synthesize step first`
(`export.py:66-70`). `--limit` caps how many rows are exported (0 = all), and
`--sample-rate` sets the WAV header rate — leave it at the MMS-TTS output rate of
16000 unless you changed the model.

## Cleanup caveat

`uv run cleanup` has no audio-specific option: its defaults target the video
pipeline tables (`videos`, `video_clips`) plus an optional
PDFs table via `--pdfs-table` (`geneva_examples/ops/cleanup.py:34-35,50-57`; see
[docs/workflows/inspecting-state.md](inspecting-state.md)). To tear the audio
example down, pick one of:

1. **Retarget cleanup** — the table names are flags, so point both at the audio
   table: `uv run cleanup --videos-table audio --clips-table audio --yes`
   (the duplicate is collapsed, so `audio` is dropped once).
2. **Overwrite in place** — re-run `uv run ingest-audio`; it recreates the table
   fresh with only `id` + `text`.
3. **Local mode: delete the database directory** — remove `./local_db` (or your
   `local_db_path` config value). This deletes every local table, not just `audio`.
4. **Targeted drop, either mode** — a short snippet with this repo's connector:

   ```python
   from geneva_examples.core.common import connect
   from geneva_examples.core.config import load_config

   connect(load_config()).drop_table("audio")
   ```
