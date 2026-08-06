# Materialized views and stable row IDs

> Part of the geneva-examples docs — index: [docs/README.md](../README.md).

## Contents

- [The clips table is the view](#the-clips-table-is-the-view)
- [Stable row IDs: the invariant](#stable-row-ids-the-invariant)
- [How this repo protects you](#how-this-repo-protects-you)
- [Refresh vs backfill](#refresh-vs-backfill)

The video example's chunk steps produce their clips table as a geneva materialized
view. This page is the warning label: a materialized view whose source table lacks
stable row IDs works exactly once and then becomes **permanently unrefreshable** —
through no user action, with no repair short of recreating the source. The
authoritative statements are the comment block around `OPT_STABLE_ROW_IDS` and the
`require_stable_row_ids` docstring in `geneva_examples/core/common.py:159-205`, plus
the comments in `geneva_examples/examples/video/chunk.py`.

## The clips table is the view

Geneva only runs a `@geneva.chunker` UDTF inside a materialized view, so the view
*is* the output table: each chunk step creates it under `--clips-table` with
`conn.create_udtf_view(...)` and fills it in place with `view.refresh(...)`. There is
no intermediary `_mv` table and no full in-memory copy of the clips
(`geneva_examples/examples/video/chunk.py:101-131` and its module docstring).

- The chunker consumes the raw `video` bytes via `input_columns`, but
  `inherit_input_columns=False` keeps them out of the view's output rows — the clips
  table never stores the movie bytes. `video_id` stays in the source projection
  without being a chunker input, so geneva inherits it onto every clip row
  (`geneva_examples/examples/video/chunk.py:101-109`).
- One view binds one source query plus one chunker. Clips from different sources or
  chunkers cannot share a table — pass a distinct `--clips-table` per variant across
  `chunk-videos`, `chunk-videos-openvid`, and `chunk-videos-external` (see
  [docs/workflows/video.md](../workflows/video.md)).
- `chunk-videos-external --detach` submits the refresh as a detached job via
  `refresh_async` and logs the job id; in local mode it is ignored with a warning
  (see [docs/workflows/video.md#detached-refresh](../workflows/video.md#detached-refresh)).
- Feature steps that add columns to the clips table (`frame-embed`, `frame-caption`,
  `frame-openpose`) must wait for the refresh to complete — see
  [docs/concepts/backfills.md#the-overlap-invariant](backfills.md#the-overlap-invariant).
- `seed-video-clips` is the exception that proves the rule: it drops the view and
  rebuilds `video_clips` as a plain table for load testing
  (`geneva_examples/examples/video/seed.py`).

## Stable row IDs: the invariant

When a chunker materialized view is created, geneva records the source table version
in the view metadata key `geneva::view::base_table_version` and never advances it
(`geneva_examples/core/common.py:181-184`). A refresh must map view rows back to
source rows across source versions, and that mapping survives a version move only if
the source has stable row IDs — the Lance write-time option that makes row IDs
survive compaction, update, and delete.

The source moving is not a user action: per the comment at
`geneva_examples/core/common.py:184-186`, LanceDB's maintenance agent compacts a
table once it passes ~30 uncompacted fragments, committing a new source version with
nobody touching the table. That threshold is a service-side behavior, not observable
in this repo's code — treat the exact number as indicative.

Consequences:

- A source without stable row IDs yields a view that refreshes once and then fails
  on every later refresh: geneva rejects it up front with `ValueError: Cannot
  refresh chunker materialized view to source version <N> because the source table
  does not have stable row IDs enabled` (verified against geneva==0.14.1b5,
  `geneva/runners/ray/pipeline.py:7112-7134`; the plain-MV equivalent is
  `geneva/table.py:2088-2120`).
- The view is then permanently unrefreshable.
- There is no retrofit: stable row IDs are write-time only, with no migration path.
  The only fix is to drop the source table, re-ingest it with stable row IDs, and
  recreate the view.

## How this repo protects you

Two guards make the failure unreachable when you stay inside the examples:

1. **Every ingest passes the option.** Each ingest step creates its table with
   `storage_options={OPT_STABLE_ROW_IDS: "true"}` (the Lance option
   `new_table_enable_stable_row_ids`), unconditionally — any table created here can
   later become a materialized-view source (`geneva_examples/core/common.py:159-175`; call
   sites include `geneva_examples/examples/video/ingest.py:69`,
   `video/ingest_openvid.py:100`, `video/ingest_external_refs.py:212`,
   `images/ingest.py:64`, `pdf/ingest.py:59`, `audio/ingest.py:79`,
   `debugging/seed_errors.py:85`, and `video/seed.py:339`).
2. **Every chunk step checks before creating the view.** `chunk-videos`,
   `chunk-videos-openvid`, and `chunk-videos-external` all call
   `require_stable_row_ids(src, source_table)` first, which raises a `RuntimeError`
   naming the table if the source lacks stable row IDs — failing at create time
   instead of on a refresh weeks later (`geneva_examples/core/common.py:178-205`;
   call sites `geneva_examples/examples/video/chunk.py:82`,
   `video/chunk_openvid.py:101`, `video/chunk_external_video.py:118`). The check
   calls `table.to_lance()` deliberately, re-reading the manifest so a cached handle
   cannot predate the ingest's own write. The error message prescribes the fix: drop
   the named source table and re-ingest.

**A false alarm to ignore:** in enterprise mode, table creation logs
`storage_options parameter is not supported when creating tables on remote
connections, ignoring`. The warning is wrong for this option: geneva's
`Connection.create_table` logs it but still forwards `storage_options` — including
`new_table_enable_stable_row_ids` — to the underlying namespace connection, which
honours it in the client-side Lance write. Verified against geneva==0.14.1b5
(`geneva/db.py:875-879` logs the warning in the `host_override` branch; the options
are still forwarded at `geneva/db.py:915-921`, which sets
`kwargs["storage_options"]` and calls `self._connect.create_table(...)` —
`self._connect` being the `LanceNamespaceDBConnection` built at
`geneva/db.py:607-628`. Note the `kwargs.update(...)` at `db.py:880-886` is the
*non*-remote branch, not the forwarding path); also noted at
`geneva_examples/core/common.py:169-174`. Do not "fix" a pipeline in response to
this line.

## Refresh vs backfill

In these docs the two words are not synonyms. A **backfill** fills a UDF-backed
column of an existing table; every feature step routes through
`geneva_examples/core/backfill.py` (see
[docs/concepts/backfills.md](backfills.md)). A **refresh** fills a materialized view
from its source table and applies only to views — the three `chunk-videos*` steps
are the only refresh callers in this repo. `chunk-pdfs`, despite its name, is a
backfill of two list columns on the `pdfs` table, not a view
(`geneva_examples/examples/pdf/chunk.py`). Term definitions, including the synonyms
you may see in code ("UDTF view", "chunker view"), live in
[docs/reference/glossary.md](../reference/glossary.md).
