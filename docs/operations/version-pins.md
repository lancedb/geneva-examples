# Version pins and cluster upgrades

> Part of the geneva-examples docs — index: [docs/README.md](../README.md).

The repo pins its LanceDB stack in two independent tiers, keeps them aligned with the
deployed Geneva driver on the cluster, and carries a small set of code paths that
depend on geneva internals. This page covers all three: the tiers, the upgrade
runbook, and the inventory of pin-fragile code to re-check after any bump.

## Contents

- [The two tiers](#the-two-tiers)
- [Why the pins must match the deployed Geneva driver exactly](#why-the-pins-must-match-the-deployed-geneva-driver-exactly)
- [Upgrade runbook](#upgrade-runbook)
- [Pin-fragility inventory](#pin-fragility-inventory)

## The two tiers

**Tier 1 — the driver (this repo's environment).** `pyproject.toml` pins `geneva`,
`lancedb`, `pylance`, and `pyarrow` with `==` to the exact versions the deployed
Geneva driver runs, locked in `uv.lock`. `numpy` is deliberately left to geneva's
own constraints
(`pyproject.toml`, the comment block above the pins). These betas resolve only from
the two explicit Gemfury indexes declared in `[[tool.uv.index]]` — public PyPI carries
geneva final releases only. CI's `uv sync --locked` is the drift gate: editing a
dependency without re-locking fails the build rather than silently moving a pin
(`.github/workflows/ci.yml`).

**Tier 2 — the worker runtime.** Remote workers install nothing from `uv.lock`; each
UDF module carries a `*_RUNTIME_PIP` list that its manifest installs. Within those
lists, `geneva`/`lancedb`/`pylance` track the versions installed on the driver
(this repo's environment) via `package_spec()`
(`geneva_examples/core/package_specs.py`), so moving tier 1
propagates to workers automatically. Every other package is pinned in its module, and
every spec can be overridden verbatim through its `{PACKAGE}_PACKAGE_SPEC` environment
variable. Do not read tier-2 values from prose — the authoritative, generated table is
[docs/reference/worker-runtime-pins.md](../reference/worker-runtime-pins.md).

Two consequences worth internalizing:

- `uv lock --upgrade` moves tier 1's unpinned dependencies but never touches tier 2's
  hardcoded specs; those are edited in the UDF modules (or overridden per-run via the
  environment variables).
- Dependabot is configured to ignore `geneva`, `lancedb`, `pylance`, `pyarrow`, and
  `numpy` by design (`.github/dependabot.yml`), so the cluster-matched pins are never
  auto-bumped. A pin only moves when a human runs the runbook below.

## Why the pins must match the deployed Geneva driver exactly

"Compatible" is not enough — the pins in this repo must **equal** the deployed
Geneva driver's versions. Both reasons are documented in the comment block above
the pins in `pyproject.toml`:

1. **Two conflicting `geneva==` pins → `ResolutionImpossible`.** geneva's Ray manager
   prepends `geneva=={its own __version__}` to the runtime-environment pip list, while
   this repo's manifests add `geneva=={installed driver version}` via
   `package_spec()`. A mismatch puts two conflicting `geneva==` pins into one
   requirements set, and pip fails with `ResolutionImpossible` before Ray even starts.
2. **`lancedb`/`pylance` skew → HTTP 500s.** Skew between the driver (this repo)
   and the deployed Geneva driver surfaces later and less legibly, as
   `declare_table`/backfill 500s.

`pyarrow` is pinned for a related reason: the stack only floors it (`pyarrow>=16`), so
left unpinned the driver environment drifts to the newest release and ends up on a
different major than the workers, whose pyarrow comes from the tier-2 manifests
(`pyproject.toml`).

Both failure modes appear as rows in
[docs/operations/troubleshooting.md](troubleshooting.md).

## Upgrade runbook

Run this whenever the data plane (the deployed Geneva driver) is upgraded. Do not
skip steps; the order matters because `make check` includes the generated-docs
freshness gate.

1. **Read the driver's versions.** The recipe from `pyproject.toml`, verbatim:

   ```bash
   kubectl -n lancedb exec <backfill-driver-pod> -c geneva-driver -- \
     /geneva_driver/.venv/bin/python -c \
     "import importlib.metadata as m; print(m.version('geneva'))"
   ```

   Repeat with `lancedb`, `pylance`, and `pyarrow` in place of `geneva`.
2. **Edit the four pins** in `pyproject.toml` (`geneva`, `lancedb`, `pylance`,
   `pyarrow`), and update the adjacent comment that records which driver release the
   versions were read from. If `pyarrow` moved, also update the
   `PYARROW_PACKAGE_SPEC` default in every manifest module that hardcodes it
   (`geneva_examples/examples/{video/chunkers.py,video/openpose.py,images/imageinfo.py,pdf/document.py,audio/tts.py,audio/transcribe.py,_shared/blip.py,_shared/clip.py}`)
   — unlike `geneva`/`lancedb`/`pylance`, tier-2 pyarrow does not track the driver
   environment, so a tier-1-only bump leaves the workers on the old major. Confirm with
   `make docs` that
   [docs/reference/worker-runtime-pins.md](../reference/worker-runtime-pins.md)
   shows the new value.
3. **Run `make lock`.** CI's `uv sync --locked` fails on any dependency edit that was
   not re-locked.
4. **Run `make docs`.**
   [docs/reference/worker-runtime-pins.md](../reference/worker-runtime-pins.md) is
   rendered from the installed environment and the manifest modules, so it embeds
   pin strings and goes stale on any bump (llms.txt / llms-full.txt inherit it). The
   generated CLI reference is unaffected by a pin change, but `make docs`
   regenerates everything in one pass.
5. **Run `make check`** (lint, format, docs freshness, tests). The test suite installs
   and imports the pinned geneva, so a bad resolve surfaces here.
6. **Smoke a real backfill** against the cluster — for example `uv run ingest-images`
   followed by `uv run lightweight` in enterprise mode — and confirm the job reaches
   `DONE` with `uv run jobs`. This is the only step that exercises failure mode 1
   (the worker-side pip resolve).
7. **Re-check the pin-fragility inventory** below; each row names its guard.

## Pin-fragility inventory

These code paths depend on geneva internals or on version-specific behavior. A pin
bump can break them without touching any public API, so each carries a guard or a
pinned test — re-verify all four after an upgrade.

| Dependency on geneva internals | Behavior if a pin bump changes it | Guard / test |
| --- | --- | --- |
| `jobs kill` cancels via the private `conn._history.set_completed` — geneva exposes no public cancel API. | Fails closed with an explicit message ("this geneva build does not expose the private jobs-history API (conn._history.set_completed) … the geneva pin may have changed") and exit code 1, never a raw `AttributeError`. | `geneva_examples/ops/jobs.py:199-208`; `tests/test_ops_jobs.py::test_kill_guards_missing_history_api` |
| Local Ray provisioning imports the private `geneva.runners.ray._mgr.ray_cluster` to keep worker-log forwarding off. | Silently falls back to the public `conn.local_ray_context()` — local runs still work but the console gets noisier, and there is no log line signaling the fallback. | `geneva_examples/core/common.py:224-233`; `tests/test_core.py::test_runtime_session_falls_back_to_public_api` |
| Job-record rendering reads a geneva-owned object whose field set can shift across pins. | Every accessor goes through `getattr` with a fallback, so a disappeared field renders as `-` instead of raising mid-render. This is a stated module invariant — preserve it in any new job-reading code. | `geneva_examples/core/jobs.py` (module docstring) |
| The enterprise "storage_options … ignoring" warning at table creation is documented as a false alarm: geneva forwards the options and the client-side Lance write honours them, so stable row IDs are applied. | Verified against geneva==0.14.1b5 (the code comment traces the forwarding path in geneva's `db.py`). If a bump makes the warning real, newly ingested tables silently lose stable row IDs and their chunker materialized views become unrefreshable later. | `geneva_examples/core/common.py:159-175`; downstream guard: `require_stable_row_ids` (`geneva_examples/core/common.py:178-205`), which hard-fails the chunk steps |

After a bump, a cheap audit for new private-API touch points:

```bash
grep -rn "_history\|ray._mgr\|_mgr import" geneva_examples/
```

Background on the view invariant the last row protects:
[docs/concepts/materialized-views.md](../concepts/materialized-views.md).
