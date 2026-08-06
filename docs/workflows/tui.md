# The interactive TUI

> Part of the geneva-examples docs — index: [docs/README.md](../README.md).

Run `uv run tui` to launch the Textual terminal app. It is a second front-end over
the same spec objects the generated CLIs use: the Examples section renders every
registered example and step as a form, and the Tables and Jobs sections read the
same database the step CLIs write. Every claim below is sourced from
`geneva_examples/tui/app.py`.

## Contents

- [Layout](#layout)
- [Keymap](#keymap)
- [The pane-aware primary action](#the-pane-aware-primary-action)
- [Retargeting](#retargeting)
- [Limits and behavior](#limits-and-behavior)
- [System tables](#system-tables)
- [How steps run](#how-steps-run)

## Layout

The left nav has three sections — **Tables**, **Jobs**, **Examples** — and the app
opens on the Tables pane with a fresh listing (`ContentSwitcher(initial="table-pane")`
at `geneva_examples/tui/app.py:245`; the `on_mount` refresh at `:305`). Rationale in
code: after a run, inspecting data is the more frequent destination than re-running.
Selecting a table shows a row grid plus a cell-detail pane underneath (highlight a
cell to see its full, untruncated value); selecting a job shows the complete record;
selecting a step shows its description, hints (`GPU model — runs on CPU in local
mode.` when `step.gpu`; `Requires: …` when `step.requires`), and a param form.

A row of global controls applies to every read and run:

| Control | Widget | Default | Notes |
|---|---|---|---|
| `#mode` | Select | `local` | `local` or `enterprise`; always explicit — no "auto" that defers to `config.yaml` (`geneva_examples/tui/app.py:72-75`). |
| `#config` | Input | empty | Path to `config.yaml` (optional in local mode). |
| `#db_uri` | Input | empty | Visible **only** in enterprise mode — local mode connects to `local_db_path` and ignores `db_uri` (`_sync_db_uri_visibility`, `geneva_examples/tui/app.py:476-483`). |
| `#log_level` | Select | `INFO` | INFO / DEBUG / WARNING / ERROR. |
| `#run` | Button | `Refresh ⟳` | The pane-aware primary action; relabels per pane (see below). |

An unusable target — enterprise mode without a `config.yaml`, or one missing
credentials — is not a crash: the nav section shows `⚠ <first 48 chars of the
error>` and the pane renders the message where the result would have gone
(`geneva_examples/tui/app.py:504-516`, `:594`). See
[docs/getting-started/configuration.md](../getting-started/configuration.md) for
what each mode requires.

## Keymap

Source: the `BINDINGS` list at `geneva_examples/tui/app.py:197-204`.

| Key | Footer label | What it does |
|---|---|---|
| `r` | Run / refresh | The pane-aware primary action — identical to clicking the button. |
| `t` | List tables | List the current backend's tables into the nav (first 10 — see [Limits](#limits-and-behavior)). |
| `j` | List jobs | List the current backend's jobs into the nav. |
| `f` | Follow job | Toggle a 3-second re-read of the selected job (Jobs pane only). |
| `d` | Detail size | Toggle the cell-detail pane between compact and expanded — for reading `geneva_errors` tracebacks (`action_toggle_detail`, `geneva_examples/tui/app.py:677-679`). |
| `q` | Quit | Exit the app. |

## The pane-aware primary action

`r` and the button run the same action, and what it does depends on the pane on
screen (`_start_run`, `geneva_examples/tui/app.py:895-920`):

| Pane | Button label | With a selection | With nothing selected |
|---|---|---|---|
| Examples | `Run ▶` | Runs the selected step as a subprocess | Does nothing |
| Jobs | `Refresh ⟳` | Re-reads the selected job record | Re-lists jobs |
| Tables | `Refresh ⟳` | Re-reads the current table (note: `refreshing`) | Re-lists tables |

The button relabels as you navigate: selecting a step or example sets `Run ▶`;
selecting a table or job sets `Refresh ⟳` (`geneva_examples/tui/app.py:321-343`).
"Nothing selected" is exactly the state a retarget leaves behind, so one press
always does something.

## Retargeting

Table names and job ids are scoped to a single database, so the control tuple
`(mode, config, db_uri)` is the app's identity of "which database is on screen"
(`_target_key`, `geneva_examples/tui/app.py:418-424`). Changing any of the three
retargets the app (`_retarget` / `_forget_browsed`,
`geneva_examples/tui/app.py:426-464`):

- Both nav listings are dropped back to a lone `↻ refresh` leaf, the current
  table/job selection is cleared, and every pane resets to its placeholder.
- **Typing** in `#config` or `#db_uri` only drops the listings — half-typed text
  is not a finished choice, so no connection is opened per keystroke. **Pressing
  Enter** retargets *and* re-lists the browsed section against the new backend
  (`geneva_examples/tui/app.py:393-405`). Picking a mode is a finished choice, so
  a mode change re-lists immediately (`:385-391`).
- In-flight reads from the old backend are discarded on arrival: each threaded
  reader carries the epoch it started with, and `_post` delivers a result only
  when that epoch still matches (`geneva_examples/tui/app.py:466-474`). A slow
  local scan cannot repaint the pane after you switch to enterprise mode.
- The header subtitle names the selected backend (`mode · db_uri · config`), so
  which database is on screen is never a guess
  (`geneva_examples/tui/app.py:444-447`).

## Limits and behavior

| Limit | Value | Source |
|---|---|---|
| Table view row cap | 100 rows — applies to **every** table | `_TABLE_ROW_LIMIT`, `geneva_examples/tui/app.py:77` |
| Nav table listing cap | 10 tables — geneva's `table_names()` defaults `limit=10`, so only the first 10 sorted names appear | `_list_tables`, `geneva_examples/tui/app.py:560`; `geneva/db.py:661-668` |
| Newest-first ordering | **Only** the system tables `geneva_jobs` / `geneva_errors` | `_fetch_newest_first`, `geneva_examples/tui/app.py:139-169` |
| Job list cap | 50 newest jobs | `_JOB_LIST_LIMIT`, `geneva_examples/tui/app.py:78` |
| Follow poll interval | 3.0 seconds | `_JOB_POLL_SECONDS`, `geneva_examples/tui/app.py:81` |
| Log drain | 10 Hz UI timer | `geneva_examples/tui/app.py:272` |

The 100-row cap and the ordering are separate facts: a regular table is read with
an unordered `search().select(cols).limit(100)`
(`geneva_examples/tui/app.py:633-639`), and the info line appends `· newest first`
only when a system table's timestamp column was actually used.

The Jobs listing queries all five statuses (PENDING / RUNNING / DONE / FAILED /
CANCELLED) — unlike `uv run jobs`, which defaults to active jobs only — and shows
a tally on the section label, e.g. `Jobs — 2 RUNNING · 1 FAILED`
(`geneva_examples/tui/app.py:713-736`). Selecting a job renders the **entire**
append-only event log (`format_detail(jr, events_limit=None)`,
`geneva_examples/tui/app.py:775`); the pane scrolls, so nothing is truncated. A
job id that does not exist reports `no such job in the <mode> database
<location>` — the usual cause is an id that belongs to the other mode's database
(`geneva_examples/tui/app.py:778-782`).

Follow (`f`) re-reads the selected job every 3 seconds — the TUI equivalent of
`uv run jobs tail` (see
[docs/workflows/inspecting-state.md](inspecting-state.md)). It refuses to start
when no job is selected or the job is already terminal, and it auto-stops when
the record reaches DONE/FAILED/CANCELLED, on any read error, and when you
navigate to any nav node outside the Jobs section
(`geneva_examples/tui/app.py:315-318`, `:829-852`). While following, the job info
line appends `· following every 3s`.

## System tables

`geneva_jobs` (job records) and `geneva_errors` (per-row backfill errors) live in
the connection's system namespace, so `table_names()` never lists them; the TUI
probes each by name via `conn.open_table(name, namespace=...)` and lists successes
as `<name> (system)` (`geneva_examples/tui/app.py:89-92`, `:561-574`).

- They are fetched newest-first by `_fetch_newest_first`
  (`geneva_examples/tui/app.py:139-169`) in two narrow passes: scan only the
  `(timestamp, key)` column pair for every matching row, sort client-side (rows
  with a null timestamp sort last), take the newest 100 keys, then re-fetch those
  rows in full with `WHERE key IN (...)`. The code comment records why: geneva
  accepts but silently ignores `order_by` on these scans, and a bare `limit()`
  would keep the *oldest* rows (`geneva_examples/tui/app.py:85-88`; verified
  against geneva==0.14.1b5).
- The `job_id` filter box appears only on system tables. Its predicate is
  `job_id LIKE '%<value>%'` — a substring match anywhere, so pasting the 8-char
  prefix from a log line works. Single quotes are stripped, not escaped, because
  job ids are hex/uuid strings (`geneva_examples/tui/app.py:613-617`). Press
  Enter to apply; blank shows all rows.
- `job_id` is moved to the first grid column on system tables — it is the key
  you filter and correlate on (`geneva_examples/tui/app.py:622-625`).

Field-by-field schemas are in
[docs/reference/tables-and-schemas.md](../reference/tables-and-schemas.md); the
error-triage walkthrough is
[docs/workflows/debugging-failed-rows.md](debugging-failed-rows.md).

## How steps run

Selecting a step under Examples builds a form from its params: bool params render
as switches, params with choices as selects, everything else as text inputs
(`geneva_examples/tui/app.py:358-381`). A **blank field means "use the step
default"** — blank values are simply omitted from the command line
(`geneva_examples/tui/app.py:938-940`). The global controls always contribute
`--mode` and `--log-level`, `--config` when non-empty, and `--db-uri` only in
enterprise mode; bool fields emit `--flag` / `--no-flag` (`_build_argv`,
`geneva_examples/tui/app.py:922-941`).

Run launches the step's own generated console script as a **subprocess**, not an
in-process thread, because Ray needs a real stdout file descriptor and Textual
captures stdout (`geneva_examples/tui/app.py:17-18`, `:943-981`). It prefers the
sibling console script next to the interpreter and falls back to
`python -c "from geneva_examples.examples.cli import <step> as c; c()"`. stderr
is merged into stdout, `PYTHONUNBUFFERED=1` keeps output line-buffered, and lines
stream through a thread-safe queue drained onto the log pane at 10 Hz. The log
shows `$ <command> <argv>`, the step's output, then `✔ <command> finished` or
`✗ <command> exited with code N`.

Per-command flags and defaults are in the generated reference — start at
[docs/reference/cli/index.md](../reference/cli/index.md). How the same spec
generates both the CLI and this form is
[docs/concepts/spec-and-cli-generation.md](../concepts/spec-and-cli-generation.md).
