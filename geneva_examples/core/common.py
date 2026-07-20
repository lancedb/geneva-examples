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
_NOISY_LOGGERS = ("ray", "lancedb", "pylance", "geneva")


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
    """Compact, display-safe string for one table cell (at most 120 chars).

    The cap matters for unbounded text — the TUI's table grid renders these
    cells verbatim, and e.g. a clips `errors` entry carries a full exception
    message.
    """
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
        rendered = f"[{n} items]" if n > 8 else repr(list(value))
    elif isinstance(value, dict):
        rendered = " ".join(f"{k}={v}" for k, v in value.items())
    else:
        rendered = str(value)
    return rendered if len(rendered) <= 120 else rendered[:119] + "…"


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


def build_manifest(config: Config, prefix: str, pip: list[str]) -> object | None:
    """Build a pinned pip manifest for remote workers, or ``None`` locally.

    Local Ray workers share the driver's environment, so no manifest/packaging is
    needed and ``@geneva.udf`` accepts ``manifest=None``.
    """
    if config.is_local:
        return None
    from geneva.manifest import GenevaManifest

    return (
        GenevaManifest.create_pip(f"{prefix}-{uuid.uuid4().hex[:6]}").pip(pip).build()
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
