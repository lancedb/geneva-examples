# Debugging failed rows: the demo-errors walkthrough

> Part of the geneva-examples docs — index: [docs/README.md](../README.md).

The debugging example manufactures the sneakiest backfill failure shape on purpose:
a job that finishes DONE while some of its rows silently failed. One step generates
real per-row errors; this page shows how to find them in the `geneva_errors` system
table, read them in the TUI, and reason about refilling the holes.

## Contents

- [What demo-errors does](#what-demo-errors-does)
- [Run it](#run-it)
- [Reading geneva_errors](#reading-geneva_errors)
- [Inspecting in the TUI](#inspecting-in-the-tui)
- [Fixing and refilling](#fixing-and-refilling)

## What demo-errors does

`demo-errors` seeds a fresh `debug_demo` table of `(id, value)` rows with
`value == id` (1 through `--rows`), then backfills a `score` column
(`float64`, `value * 1.5`) with a deliberately faulty UDF
(`geneva_examples/examples/debugging/seed_errors.py`). The failures are
deterministic (`geneva_examples/examples/debugging/faulty.py:44-51`):

| Rule | Exception raised | Plays the role of |
|---|---|---|
| `value % fail_every == 0` (`--fail-every`; 0 disables) | `ValueError` | corrupt input rows |
| `value % 10 == 9` | `TimeoutError` | flaky I/O |

The `ValueError` rule is checked first, so a value matching both raises
`ValueError`. With the shipped defaults (see the flag table) that is 5
`ValueError` rows (7, 14, 21, 28, 35) and 4 `TimeoutError` rows (9, 19, 29,
39) — 9 failures you can predict from the error messages alone.

The UDF declares `on_error=skip_on_error()`: each failing row is written as NULL
in `score` and recorded in the `geneva_errors` system table, while the backfill
job itself finishes **DONE**. That "success with holes" shape is exactly what a
status check misses. The UDF is scalar here for readability
(`faulty.py:44-51`), but the hook for retrying only the failed rows comes from
`on_error=skip_on_error()` itself: that selects geneva's SKIP_ROWS fault
isolation, which applies rows one at a time and stamps a `row_address` on each
error record. Array (`pa.Array`) UDFs get the same isolation; only RecordBatch
UDFs are rejected by SKIP_ROWS.

After the backfill, the step prints the NULL count, the error records grouped by
`error_type`, and the job id with copy-paste next steps. It scopes the error read
to the newest job for the table because `geneva_errors` is append-only — records
accumulate across re-runs (`seed_errors.py:112-121`).

## Run it

```bash
uv run demo-errors
```

This is the only step whose spec pins `default_mode="local"`
(`geneva_examples/examples/debugging/__init__.py:26`): with no `--mode` flag it
always runs on the laptop, regardless of `config.yaml`. Pass `--mode enterprise`
to opt out — it then warns that it is about to overwrite `debug_demo` on the
cluster (`seed_errors.py:69-74`). The run pushes a few dozen rows through a
trivial UDF — wall time is dominated by local Ray startup — and ends with the
sentinel `demo_errors_ok`. Full flag table:
[docs/reference/cli/debugging.md#demo-errors](../reference/cli/debugging.md#demo-errors).

## Reading geneva_errors

`geneva_errors` is one of geneva's system tables; it and `geneva_jobs` (the job
records — see [docs/workflows/inspecting-state.md](inspecting-state.md)) are the
two the TUI surfaces after a backfill. Both live in the connection's system
namespace and are never listed by `table_names()`
(`geneva_examples/tui/app.py:561-570`).

Each record is one failed UDF attempt. The fields are defined by geneva's
`ErrorRecord` (`geneva/debug/error_store.py` inside the installed geneva package),
verified against geneva==0.14.1b5:

| Field | Meaning |
|---|---|
| `error_id` | unique id (UUID) of this record |
| `error_type` | exception class name — `ValueError`, `TimeoutError`, … |
| `error_message` | the exception message |
| `error_trace` | the full Python traceback |
| `job_id` | the backfill job that hit the error — the key you filter and correlate on |
| `table_name` / `table_uri` / `table_version` | the table (and read version) being backfilled |
| `column_name` | the output column being computed (`score` here) |
| `udf_name` | for a column backfill this is the *output column* key (`score` here), not the UDF function name (geneva records `map_task.name()`) |
| `udf_version` | geneva's checkpoint prefix for the binding, not the `version=` value the UDF factory sets |
| `row_address` | physical address of the failing row; set whenever the UDF's `on_error` policy is `skip_on_error()` — geneva then applies rows one at a time for scalar and array UDFs alike. NULL on the fail-fast and batch-retry paths; `skip_on_error()` is rejected outright for RecordBatch UDFs |
| `attempt` / `max_attempts` | retry counters (1 / 1 when no retry policy is configured) |
| `batch_index` / `fragment_id` / `actor_id` / `bisect_depth` / `input_columns` / `output_columns` | execution context within the job |
| `timestamp` | when the error was recorded (UTC) |

Because the table is append-only, always scope a read to one job: the demo prints
its job id, and programmatic reads pass it to `table.get_errors(job_id=...)` — the
call `seed_errors.py` itself makes.

## Inspecting in the TUI

Run `uv run tui` and open the Tables section: the viewer probes the system
namespace and lists `geneva_jobs (system)` and `geneva_errors (system)` next to
your regular tables. Selecting a system table reveals a **job_id filter** box
above the grid — plain tables do not get one (`geneva_examples/tui/app.py:535-537`).

The filter is a substring match: the value has quotes stripped and is wrapped as
`job_id LIKE '%<value>%'` (`tui/app.py:613-617`), so pasting just the first few
characters of the job id the demo printed is enough to isolate that run.

The grid shows at most 100 rows, newest first for system tables (`geneva_errors`
sorts on `timestamp`). Cells are truncated to one line; highlight a cell to see
its full value in the detail pane below the grid, and press `d` to grow the pane —
that is how you read a complete `error_trace` (`tui/app.py:202`, `677-679`). Open
the `debug_demo` table too: the failed rows are the ones with a NULL `score`. The
full keymap and viewer limits are in [docs/workflows/tui.md](tui.md); the
terminal alternative is `uv run jobs show <job_id>`, which prints the raw
`geneva_jobs` record.

## Fixing and refilling

The two failure roles call for different refills. The contract lives in
`geneva_examples/core/backfill.py`; the concept page is
[docs/concepts/backfills.md](../concepts/backfills.md).

- **Transient failures** (the `TimeoutError` role): the UDF is fine, the rows just
  need another pass. An incremental backfill (`reset=False`) fills exactly the
  rows where the output column IS NULL, reusing the column's registered UDF.
- **Deterministic bugs** (the `ValueError` role): re-running the same UDF re-fails
  the same rows. Fix the UDF, then rebuild with `reset=True` — incremental mode
  keeps the originally registered UDF, so a fixed UDF never takes effect without
  a reset (`backfill.py:113-118`).

The `row_address` on each error record identifies exactly which physical rows
failed, which is what makes retry-only-the-failed-rows tractable in a real
pipeline. The demo itself exposes no `--reset` flag: every `uv run demo-errors`
run recreates the table and recomputes `score` from scratch, so re-running it
starts a new demo rather than refilling the previous one.
