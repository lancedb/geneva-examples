"""Shared CLI helpers: logging, the Geneva connection, and mode-aware helpers.

Geneva powers both the ``local`` and ``enterprise`` modes (see
:mod:`geneva_examples.core.config`). The helpers here concentrate every place the
two modes differ so the ingest/stage CLIs stay almost identical:

* :func:`connect` — a local on-disk ``NativeConnection`` vs. the cloud
  ``RemoteConnection``.
* :func:`runtime_session` — provisions a local Ray instance for the duration of a
  local backfill; a no-op in enterprise mode.
* :func:`build_manifest` — a pinned pip manifest for remote workers, or ``None``
  locally (local Ray workers share the driver's env).
* :func:`resolve_resources` — clamps GPU/CPU requests so local Ray can actually
  schedule the task on a laptop.
* :func:`local_or` — pick a small local default vs. the cloud-tuned value.
"""

from __future__ import annotations

import logging
import os
import uuid
import warnings
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Any

from geneva_examples.core.config import Config

logger = logging.getLogger(__name__)

# Geneva serializes a UDF/chunker's Ray `memory` request into a signed 32-bit
# field on the namespace API; values >= 2**31 raise OverflowError. `memory` is an
# advisory Ray scheduling reservation, so capping it is safe.
_MEMORY_MAX_BYTES = 2**31 - 1

# Third-party loggers that flood the console at INFO with per-fragment/namespace
# chatter. Quieted to WARNING unless the user asks for --log-level DEBUG.
# httpx/huggingface_hub/sentence_transformers are here because the model stages
# log one INFO line per HF cache probe — dozens of them before a single embedding
# is computed, which buries the step's own output.
_NOISY_LOGGERS = (
    "ray",
    "lancedb",
    "pylance",
    "geneva",
    "httpx",
    "huggingface_hub",
    "sentence_transformers",
    "transformers",
)


def setup_logging(level: str = "INFO") -> None:
    """Configure logging for a CLI/TUI run and quiet the noisy dependencies.

    Keeps ``geneva_examples`` (our own logs) at ``level`` while dropping the
    verbose INFO chatter from ray/lancedb/geneva and lance's Rust event logs
    (via ``LANCE_LOG``), plus the noisy lancedb fork ``RuntimeWarning``. Pass
    ``--log-level DEBUG`` to see everything again.
    """
    lvl = level.upper()
    # Silence lance's Rust `lance::events::*` INFO stream at the source; must be
    # set before lance is imported (workers inherit it from the driver env).
    if lvl != "DEBUG":
        os.environ.setdefault("LANCE_LOG", "warn")
    logging.basicConfig(
        level=lvl,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("geneva_examples").setLevel(lvl)
    if lvl != "DEBUG":
        for name in _NOISY_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)
        warnings.filterwarnings(
            "ignore", message="lancedb fork support is experimental"
        )


def format_sample(rows: list[dict[str, Any]], columns: list[str] | None = None) -> str:
    """Render a list of row dicts as a compact aligned table for logging.

    Long/opaque values are summarized (embeddings → ``[512 floats]``, bytes →
    ``<N B>``, structs → ``k=v``) so a feature preview reads cleanly instead of
    dumping raw Python.
    """
    if not rows:
        return "(no rows)"
    cols = columns or list(rows[0].keys())
    cells = [[format_cell(r.get(c)) for c in cols] for r in rows]
    widths = [
        min(40, max(len(c), *(len(row[i]) for row in cells)))
        for i, c in enumerate(cols)
    ]
    line = "  ".join(c.ljust(w) for c, w in zip(cols, widths, strict=False))
    sep = "  ".join("-" * w for w in widths)
    body = "\n".join(
        "  ".join(v[:w].ljust(w) for v, w in zip(row, widths, strict=False))
        for row in cells
    )
    return f"{line}\n{sep}\n{body}"


def format_cell(value: Any) -> str:
    """Compact, display-safe string for one table cell."""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} B>"
    if isinstance(value, float):
        return f"{value:.4g}"
    if isinstance(value, (list, tuple)):
        n = len(value)
        if n > 8 and all(isinstance(x, (int, float)) for x in value):
            return f"[{n} floats]"
        return f"[{n} items]" if n > 8 else repr(list(value))
    if isinstance(value, dict):
        return " ".join(f"{k}={v}" for k, v in value.items())
    text = str(value)
    # Multiline or very long values (e.g. geneva_errors tracebacks) get a
    # single bounded line so the table grid stays readable.
    first, _, rest = text.partition("\n")
    if rest:
        first = f"{first} …"
    if len(first) > 120:
        first = first[:119] + "…"
    return first


def memory_request_bytes(gib: float) -> int:
    """Bytes for a Ray `memory` request, capped to geneva's 32-bit field limit."""
    requested = int(gib * 1024**3)
    if requested > _MEMORY_MAX_BYTES:
        logger.warning(
            "memory request %.1f GiB exceeds geneva's 32-bit field; capping to %d bytes (~%.2f GiB)",
            gib,
            _MEMORY_MAX_BYTES,
            _MEMORY_MAX_BYTES / 1024**3,
        )
        return _MEMORY_MAX_BYTES
    return requested


def connect(config: Config):
    """Open a Geneva connection from the resolved ``config``.

    Local mode connects to an on-disk Lance database (a ``NativeConnection``);
    enterprise mode connects to LanceDB Cloud + the remote Geneva runtime (a
    ``RemoteConnection``). The connection class is chosen by geneva from the URI:
    a ``Path`` is local, ``db://…`` is remote.
    """
    import geneva

    if config.is_local:
        local_path = Path(config.local_db_path).expanduser()
        logger.info("connecting local geneva at %s", local_path)
        return geneva.connect(
            uri=local_path,
            storage_options=config.storage_options(),
        )

    return geneva.connect(
        uri=config.db_uri,
        host_override=config.geneva_host,
        api_key=config.lancedb_api_key,
        region=config.lancedb_region,
        storage_options=config.storage_options(),
    )


# Lance write option that makes row IDs survive compaction/update/delete. Every
# table an example creates passes it to ``conn.create_table``:
#
#     conn.create_table(name, data=..., storage_options={OPT_STABLE_ROW_IDS: "true"})
#
# A chunker materialized view can only refresh across source versions when its
# SOURCE table has these; without them the view is orphaned the first time the
# source moves -- including by the maintenance agent's own compaction. They are
# write-time only (no migration, only a full rewrite), and any table here can
# later become a chunker/UDTF view source, so they go on unconditionally.
#
# Enterprise mode logs "storage_options parameter is not supported when creating
# tables on remote connections, ignoring" -- a false alarm. ``Connection`` forwards
# the options to the ``LanceNamespaceDBConnection`` anyway (geneva 0.14.0 db.py),
# which honours them in the client-side Lance write it performs after asking
# phalanx for the table location.
OPT_STABLE_ROW_IDS = "new_table_enable_stable_row_ids"


def require_stable_row_ids(table, table_name: str) -> None:
    """Refuse to build a chunker materialized view over an unsuitable source.

    A chunker MV records the source version it was built against in
    ``geneva::view::base_table_version`` and never advances it, so the first time
    the source moves the view becomes permanently unrefreshable unless the source
    has stable row IDs. The source moving is not a user action: the maintenance
    agent compacts any table past 30 uncompacted fragments on its own, which
    commits a new version.

    So a source without stable row IDs yields a view that works once and then dies
    somewhere else entirely, hours later, in the indexing subsystem. Fail here
    instead, naming the table -- there is no retrofit for stable row IDs
    (write-time only, no migration), so the table has to be recreated.
    """
    from geneva.db import dataset_uses_stable_row_ids

    # to_lance() re-reads the manifest; a cached handle can predate our own write.
    if dataset_uses_stable_row_ids(table.to_lance()):
        return

    raise RuntimeError(
        f"source table {table_name!r} does not have stable row IDs, so a chunker "
        "materialized view over it cannot be refreshed once the source version "
        "moves -- which the maintenance agent's compaction does unprompted. "
        f"Drop {table_name!r} and re-ingest; this example now creates sources with "
        f"{OPT_STABLE_ROW_IDS}=true."
    )


def runtime_session(conn: object, config: Config) -> AbstractContextManager:
    """Context wrapping a run's backfills.

    In local mode this provisions a single local Ray instance for the whole run
    (Ray is torn down on exit, so it must wrap the entire backfill loop, not each
    column). In enterprise mode it is a no-op — work runs on the remote cluster.

    Unlike ``conn.local_ray_context()`` (which hardcodes ``log_to_driver=True``),
    we turn Ray worker-log forwarding **off** and set Ray's own logging to WARNING
    so the console isn't flooded with per-fragment ``lance::events`` chatter. Pass
    ``--log-level DEBUG`` to get the full worker logs back. Falls back to the
    public context manager if geneva's internal API changes.
    """
    if not config.is_local:
        return nullcontext()
    verbose = logging.getLogger("geneva_examples").getEffectiveLevel() <= logging.DEBUG
    try:
        from geneva.runners.ray._mgr import ray_cluster

        return ray_cluster(
            local=True,
            log_to_driver=verbose,
            logging_level=logging.DEBUG if verbose else logging.WARNING,
        )
    except Exception:  # noqa: BLE001 - degrade to the public (noisier) API
        return conn.local_ray_context()  # type: ignore[attr-defined]


# conda-forge packages every remote worker environment gets, regardless of
# which UDF's pip deps ride alongside them via build_manifest(). Ray's
# runtime_env accepts pip XOR conda, never both, so build_manifest folds the
# caller's pip list into conda's nested ``pip:`` key instead of setting a
# separate top-level ``pip:`` -- see the ffmpeg CLI example in
# geneva_examples/examples/debugging/ffmpeg_probe.py.
COMMON_CONDA_DEPENDENCIES = [
    "python=3.12",
    os.environ.get("FFMPEG_PACKAGE_SPEC", "ffmpeg=8.1.2"),
]

# Geneva/lancedb/pylance betas live on Gemfury, not public PyPI (see the
# [tool.uv.index] comment in pyproject.toml). Ray's pip runtime_env picks this
# up via a PIP_EXTRA_INDEX_URL env var (geneva._mgr._EXTRA_PIP_INDEX_URLS), but
# conda's own internal `pip install -r requirements.txt` subprocess doesn't see
# that env var -- it must be a literal requirements-file directive, which is
# why this has to be a plain list entry rather than an env var here too.
_CONDA_PIP_EXTRA_INDEX_URLS = [
    "--extra-index-url=https://pypi.fury.io/lancedb/",
    "--extra-index-url=https://pypi.fury.io/lance-format/",
]


def build_manifest(config: Config, prefix: str, pip: list[str]) -> object | None:
    """Build a pinned conda manifest for remote workers, or ``None`` locally.

    Local Ray workers share the driver's environment, so no manifest/packaging is
    needed and ``@geneva.udf`` accepts ``manifest=None``.

    Goes through conda (not a plain pip manifest) so every caller's workers also
    get ``COMMON_CONDA_DEPENDENCIES`` (e.g. the ``ffmpeg`` CLI) without each UDF
    module having to ask for it individually. ``pip`` is nested inside conda's
    dependency list -- nothing is auto-injected the way it is for a pip-only
    manifest, so this list must carry everything the worker needs, including
    ``geneva`` itself.
    """
    if config.is_local:
        return None
    from geneva.manifest import GenevaManifest

    conda_env = {
        "channels": ["conda-forge"],
        "dependencies": [
            *COMMON_CONDA_DEPENDENCIES,
            "pip",
            {"pip": [*_CONDA_PIP_EXTRA_INDEX_URLS, *pip]},
        ],
    }
    return (
        GenevaManifest.create_conda(f"{prefix}-{uuid.uuid4().hex[:6]}")
        .conda(conda_env)
        .build()
    )


def total_ram_bytes() -> int | None:
    """Best-effort total physical RAM in bytes (POSIX); ``None`` if unknown."""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):  # pragma: no cover - platform gap
        return None


def resolve_resources(
    config: Config,
    *,
    num_cpus: float,
    num_gpus: float | None,
    memory_gib: float,
) -> tuple[float, float | None, int]:
    """Return ``(num_cpus, num_gpus, memory_bytes)`` clamped for the mode.

    Enterprise mode passes the requests through (they target the GPU cluster). In
    local mode there is usually no GPU and only a handful of cores, so a task that
    reserves ``num_gpus>0`` or more CPUs than the machine has would never be
    scheduled by local Ray. We force ``num_gpus=0``, cap ``num_cpus`` to the local
    core count, and cap the (advisory) Ray ``memory`` reservation to a quarter of
    RAM so a small box (e.g. 2 GB / 4 cores) can still schedule the task. Actual
    footprint is bounded by concurrency — model steps run one actor at a time
    locally (see ``local_or`` on ``concurrency``).
    """
    memory_bytes = memory_request_bytes(memory_gib)
    if not config.is_local:
        return num_cpus, num_gpus, memory_bytes
    cpu_cap = float(max(1, min(int(num_cpus), os.cpu_count() or 1)))
    ram = total_ram_bytes()
    if ram is not None:
        memory_bytes = min(memory_bytes, max(256 * 1024**2, int(ram * 0.25)))
    return cpu_cap, 0, memory_bytes


def local_or[T](config: Config, local_value: T, enterprise_value: T) -> T:
    """Pick a small local default vs. the cloud-tuned value based on mode."""
    return local_value if config.is_local else enterprise_value


def local_concurrency(requested: int) -> int:
    """Cap backfill/refresh concurrency for a local run.

    Local Ray only has this machine's cores, so the cloud-tuned default (e.g. 32)
    would massively oversubscribe. We cap to ``cpu_count - 1``, leaving a core for
    the raylet/driver, with a floor of 1 (so a 1–2 core box still runs).
    """
    return max(1, min(requested, (os.cpu_count() or 1) - 1))
