# Inspecting state: stats, jobs, cleanup

> Part of the geneva-examples docs — index: [docs/README.md](../README.md).

Three operator CLIs read — and, for `jobs kill` and `cleanup`, mutate — the same
database the pipelines write. They are hand-written Typer apps, not generated from
the spec, so this page is their authoritative flag reference (the generated pages
under `docs/reference/cli/` carry flag tables only for the spec-generated pipeline
commands; the command index lists these three as one-line entries). All
three share the four connection flags `--config`, `--mode`, `--db-uri`,
`--log-level`; only the `--log-level` default differs per tool. None of them offers
JSON or any other machine-readable output — see [Known warts](#known-warts).

## Contents

- [stats](#stats)
- [jobs](#jobs)
- [jobs show / tail / kill](#jobs-show--tail--kill)
- [The job record](#the-job-record)
- [The system tables](#the-system-tables)
- [cleanup](#cleanup)
- [Known warts](#known-warts)

## stats

Run `uv run stats` — a single-command app, no subcommand. Per table it prints the
row count, schema, feature-column population, per-modality stats, and a caption
sample. Source: `geneva_examples/ops/stats.py`.

| Flag | Type | Default | Effect |
|---|---|---|---|
| `--config` | path | `None` (→ `./config.yaml`) | Path to config.yaml. |
| `--mode` | str | `None` | `local` or `enterprise`; overrides the config file. |
| `--db-uri` | str | `None` | Overrides the config `db_uri` (enterprise mode only). |
| `--log-level` | str | `WARNING` | Quiet by default so the report is the only output. |
| `--table` | str, repeatable | `images`, `videos`, `video_clips` | Tables to summarize; repeat the flag per table. |
| `--sample` | int | `5` | Caption rows to preview; `0` skips the preview. |
| `--max-rows` | int | `100000` | Caps the client-side scan used for per-video stats. |

The default table set covers only the image and video workflows: `pdfs`, `audio`,
and `debug_demo` each need an explicit `--table` (`_DEFAULT_TABLES`,
`geneva_examples/ops/stats.py:31`). A missing table prints `(table not found)` and
the run continues.

Output shape (plain text):

- Header: `mode: <mode>   location: <path or uri>` — the location is
  `local_db_path` in local mode and `db_uri` in enterprise mode
  (`geneva_examples/ops/stats.py:166-167`).
- Per table: `[<name>]`, then `rows: N` and one `name: type` schema line per
  field.
- Feature columns: for each of `file_size`, `dimensions`, `embedding`, `caption`,
  `pose`, `caption_blip` present in the schema, `<col>: <populated>/<total>
  populated`, computed as total minus `count_rows("col IS NULL")`
  (`geneva_examples/ops/stats.py:20-27`, `:46-58`). With none present it prints
  `feature columns: none yet`.
- Clips branch: when `video_id`, `start_sec`, and `end_sec` are all present, it
  prints clips-per-video (first 5, sorted, with a `(+N more)` suffix) and
  `chunk seconds: count=… total=… min=… max=… avg=…`; when the scan hit
  `--max-rows` it notes `(per-video stats sampled from the first N of M rows)`
  (`geneva_examples/ops/stats.py:70-102`).
- Otherwise: a 5-value sample of the first id column found among `video_id`,
  `image_id`, `doc_id`.
- Caption sample: the first of `caption` / `caption_blip`, keyed by whichever of
  `video_id`, `chunk_id`, `image_id` exist (`geneva_examples/ops/stats.py:105-115`).

## jobs

Run `uv run jobs` to list jobs. The list command is the Typer callback
(`@app.callback(invoke_without_command=True)`, `geneva_examples/ops/jobs.py:53`),
so listing needs no subcommand; `show`, `kill`, and `tail` are subcommands. The
default scope is **active** jobs only (PENDING and RUNNING).

| Flag | Type | Default | Effect |
|---|---|---|---|
| `--config` | path | `None` (→ `./config.yaml`) | Path to config.yaml. |
| `--mode` | str | `None` | `local` or `enterprise`; overrides the config file. |
| `--db-uri` | str | `None` | Overrides the config `db_uri` (enterprise mode only). |
| `--log-level` | str | `WARNING` | Quiet by default. |
| `--job-id` | str | `None` | Print the full record for one job id — an **exact, full-id** lookup, same rendering as `show`. |
| `--full-events` | flag | off | With `--job-id`: print the entire event log instead of the last 10 events. |
| `--table` | str | `None` | Only jobs targeting this table name. |
| `--status` | str | `None` | Exact status filter (upper-cased): PENDING / RUNNING / DONE / FAILED / CANCELLED. |
| `--all` | flag | off | Include terminal jobs (default scope: PENDING/RUNNING only). |
| `--limit` | int | `50` | Max rows displayed. |

The list output is a header line `db_uri: <uri>   filter: <scope>   showing: k/n`
followed by the column header
`STATUS    TYPE         ELAPSED  LAUNCHED (UTC)       TARGET / JOB`, a dashed rule,
and one row per job: status, job type, elapsed as `H:MM:SS`, the UTC launch stamp,
the `table.column` target, and the full job id
(`geneva_examples/ops/jobs.py:104-119`). No matching jobs prints
`(no matching jobs)`.

Every job id argument on this CLI (`--job-id`, `show`, `kill`, `tail`) is an exact
match: the lookup is geneva's `conn.get_job`, which queries
`job_id = '<id>'` (verified against geneva==0.14.1b5). Substring matching exists
only in the TUI's system-table filter box — see
[docs/workflows/tui.md](tui.md#system-tables).

## jobs show / tail / kill

All three subcommands take the job id as a positional argument and accept the four
connection flags (`--log-level` default `WARNING`). A missing id prints
`job <id> not found on <db_uri>` to stderr in red and exits 1
(`geneva_examples/ops/jobs.py:144-146`).

| Subcommand | Flag | Default | Effect |
|---|---|---|---|
| `show` | `--full-events` | off | Print the entire event log instead of the default 10-event tail. |
| `tail` | `--interval` | `2.0` | Poll interval in seconds; floored at 0.5 (`max(0.5, interval)`, `geneva_examples/ops/jobs.py:271`). |
| `tail` | `--once` | off | Print the current state once and exit (no follow). |
| `kill` | `--force` | off | Mark CANCELLED even if the job is already terminal. |
| `kill` | `--yes` / `-y` | off | Skip the confirmation prompt. |

`jobs show <id>` is the positional equivalent of the top-level `--job-id` option:
the full record with a 10-event tail by default, everything with `--full-events`.

`jobs tail <id>` polls the record's append-only `events` list — geneva exposes no
streaming log API — printing each new event, a `[status: X]` line whenever the
status changes, and `[metrics: …]` whenever the metrics line changes. It exits
when the job reaches a terminal state (or immediately with `--once`) and then
prints the full record; Ctrl-C stops it cleanly
(`geneva_examples/ops/jobs.py:215-278`).

`jobs kill <id>` flips the job record to CANCELLED. With a terminal job and no
`--force` it prints `… is already <status>; nothing to cancel` and exits 0.
Otherwise it prompts `Cancel <status> job <id> (<target>)?` — declining aborts
with a non-zero exit — unless `-y` is passed.

The kill mechanism is a **private geneva API**: geneva has no public cancel, so
`kill` calls `conn._history.set_completed(job_id, status="CANCELLED")`
(`geneva_examples/ops/jobs.py:196-212`). The access is guarded — if a geneva pin
bump renames or removes the attribute, `kill` fails with an explicit "this geneva
build does not expose the private jobs-history API" message and exit code 1
instead of a raw `AttributeError`. This is a known pin fragility; see the
inventory in [docs/operations/version-pins.md](../operations/version-pins.md).

What `kill` does **not** do: stop in-flight compute. A PENDING job is stopped
before dispatch, but a RUNNING backfill's in-flight Ray worker tasks may keep
going until they finish or time out — only the record reads CANCELLED
(`geneva_examples/ops/jobs.py:167-174`).

## The job record

A job record is the only progress-and-post-mortem surface a backfill or a
materialized-view refresh exposes. Geneva has no streaming log API, so **the
record's append-only event list is the log**. The querying and rendering contract
shared by this CLI and the TUI's Jobs pane lives in
`geneva_examples/core/jobs.py` — see its module docstring for the authoritative
statement.

Status vocabulary (`geneva_examples/core/jobs.py:23-25`):

| Status | Class | Meaning |
|---|---|---|
| PENDING | active | Recorded but not yet dispatched. |
| RUNNING | active | Compute in flight. |
| DONE | terminal | Finished successfully (rows skipped by `skip_on_error` still count as DONE). |
| FAILED | terminal | The job itself failed. |
| CANCELLED | terminal | Flipped by `jobs kill` (or geneva); terminal records never change again. |

A backfill record carries roughly 35 metrics, most of them timers and running
totals whose `total` is 0. The one-line progress summary keeps only ratio-shaped
metrics (`total > 0`), prefers `rows_committed`, `tasks_completed`, and
`fragments`, and caps at three (`geneva_examples/core/jobs.py:81-107`); the full
detail view prints every metric as `name: n/total desc`.

The full record rendering (`format_detail`,
`geneva_examples/core/jobs.py:136-187`) prints: job_id, status, type, target
(`table.column`), cluster, launched (stamp + by), updated, completed, elapsed
(`H:MM:SS`), then — when present — `object_ref`, the manifest id + checksum, the
pretty-printed launch config (geneva stores it as JSON text), all metrics, and
the events block labeled `events (N total)` — `, showing last K` is appended
only when the tail actually hid events.

Two robustness notes, both deliberate:

- Every field access goes through `getattr` with a fallback, so a field that
  disappears across a geneva pin bump degrades to `-` rather than raising
  mid-render (`geneva_examples/core/jobs.py:1-11`).
- Listing queries geneva once per status and merges by job id, so one status
  whose query errors is logged and skipped rather than sinking the whole
  listing (`geneva_examples/core/jobs.py:190-204`). (In geneva 0.14.1b5
  `JobStateManager.list_jobs` guards its `WHERE` with `if wheres:`, so a
  no-filter call is valid — the per-status loop is defensive, not required.)

## The system tables

Job records live in the `geneva_jobs` table and per-row backfill errors in
`geneva_errors`. Both are LanceDB tables in the connection's **system
namespace**, so `conn.table_names()` never lists them; open them with
`conn.open_table(name, namespace=list(conn.system_namespace))`
(`geneva_examples/tui/app.py:131-136`).

The canonical name is `geneva_jobs` — geneva's own constant is
`GENEVA_JOBS_TABLE_NAME = "geneva_jobs"` (verified against geneva==0.14.1b5).
Docstrings that mention `_geneva_jobs` (including geneva's) refer to the same
table.

To browse them interactively, use the TUI's Tables pane, which lists them with a
`(system)` suffix, orders them newest-first, and offers a `job_id` substring
filter — see [docs/workflows/tui.md](tui.md#system-tables). Field-by-field
schemas are in
[docs/reference/tables-and-schemas.md](../reference/tables-and-schemas.md), and
the `geneva_errors` triage workflow is
[docs/workflows/debugging-failed-rows.md](debugging-failed-rows.md).

## cleanup

Run `uv run cleanup` — a single-command app that drops the video workflow's
tables so a fresh ingest/chunk run starts clean. Source:
`geneva_examples/ops/cleanup.py`.

| Flag | Type | Default | Effect |
|---|---|---|---|
| `--config` | path | `None` (→ `./config.yaml`) | Path to config.yaml. |
| `--mode` | str | `None` | `local` or `enterprise`; overrides the config file. |
| `--db-uri` | str | `None` | Overrides the config `db_uri` (enterprise mode only). |
| `--log-level` | str | `INFO` | Louder than stats/jobs so each drop is logged. |
| `--videos-table` | str | `videos` | Videos table to drop. |
| `--clips-table` | str | `video_clips` | Clips table to drop. |
| `--pdfs-table` | str | `None` | Also drop this table (e.g. `pdfs`). |
| `--yes` / `-y` | flag | off | Skip the confirmation prompt. |

The target list is exactly `<videos>`, `<clips>`, plus the `--pdfs-table` value
when given, de-duplicated while preserving order
(`geneva_examples/ops/cleanup.py:50-57`). Each flag drops only the table it
names — there are no derived siblings, because
`conn.create_udtf_view(clips_table, …)` makes the view *be* the clips table
rather than building a separate `<name>_mv` one
(`geneva_examples/examples/video/chunk.py:110-118`; see
[docs/concepts/materialized-views.md](../concepts/materialized-views.md)). To
clear a table the flags do not cover, retarget them (e.g.
`--videos-table audio --clips-table audio`) or drop it directly — see [The
teardown gap](#the-teardown-gap).

It prints the target list, asks `Proceed? This permanently deletes the tables.`
unless `--yes`, then drops each one. A missing table is skipped and logged as
`skip_missing <name>`; a successful drop logs `dropped <name>`; the run ends with
`cleanup_ok`. Like every generated step CLI, it sets
`RAY_ENABLE_UV_RUN_RUNTIME_ENV=0` before connecting
(`geneva_examples/ops/cleanup.py:45`).

### The teardown gap

`cleanup` knows nothing about the `images`, `audio`, and `debug_demo` tables —
there is no generic teardown command in this repo. Workarounds:

- **Local mode**: delete the on-disk database directory, `rm -rf ./local_db`
  (the default `local_db_path`; `geneva_examples/core/config.py:34`). This
  removes every table at once, including the system tables.
- **Enterprise mode**: drop the tables explicitly through the same connection
  helper the pipelines use:

  ```python
  from geneva_examples.core.common import connect
  from geneva_examples.core.config import load_config

  conn = connect(load_config(None))  # reads ./config.yaml
  for name in ("images", "audio", "debug_demo"):
      try:
          conn.drop_table(name)
          print("dropped", name)
      except Exception as exc:
          print("skipped", name, "-", exc)
  ```

A teardown is often unnecessary before a re-run: most ingest steps can overwrite
their table (see each step's `--overwrite` default in the generated reference,
starting at [docs/reference/cli/index.md](../reference/cli/index.md)).

## Known warts

- **`jobs` prints `db_uri:` even in local mode.** The list header
  (`db_uri: <uri> …`), `tail`'s `tailing job <id> (<target>) on <db_uri>`
  banner, and every `not found on <db_uri>` message name `cfg.db_uri`
  unconditionally (`geneva_examples/ops/jobs.py:105`, `:245`, `:90`), but
  local mode connects to `local_db_path` and never reads `db_uri` — the printed
  value does not name the database that was actually queried. `stats` and the
  TUI print a mode-aware location instead (`geneva_examples/ops/stats.py:166-167`).
- **No machine-readable output.** No ops CLI offers `--json`, `--format`, or any
  structured output mode — output is human-oriented plain text only. Agents must
  parse the text shapes documented on this page and must not expect a JSON flag
  to exist.
- **`--status` is not validated.** `jobs --status foo` upper-cases the value and
  queries it as-is; an unknown status simply yields an empty listing
  (`geneva_examples/ops/jobs.py:95-96`).
