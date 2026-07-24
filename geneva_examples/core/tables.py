"""Shared table-viewer logic: system tables, newest-first reads, cell text.

Backs both the TUI table viewer (``geneva_examples.tui.app``) and the
``tables`` ops CLI, so the two surfaces list, filter, and order rows the same
way. Everything here is a plain function over a geneva connection/table —
no UI, no typer — and is unit-tested against fakes.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_ROW_LIMIT = 100

# Geneva system tables worth browsing after a backfill: the job records and
# the per-row error store. They live in the connection's system namespace.
# Each maps to (timestamp column, unique key column): viewers scan just that
# narrow pair, sort newest-first, then fetch the top rows by key — geneva
# 0.14 accepts but ignores order_by on these scans, so sorting server-side
# isn't an option and a bare limit() would keep the oldest rows.
SYSTEM_TABLES = {
    "geneva_jobs": ("launched_at", "job_id"),
    "geneva_errors": ("timestamp", "error_id"),
}


def open_any_table(conn: Any, name: str, *, system: bool = False) -> Any:
    """Open a regular table, or a geneva system table via its namespace."""
    if not system:
        return conn.open_table(name)
    namespace = list(getattr(conn, "system_namespace", None) or [])
    return conn.open_table(name, namespace=namespace)


def probe_system_tables(conn: Any) -> list[str]:
    """The system tables that exist on this connection.

    Each is absent until the first job/error creates it, so probe rather
    than assume.
    """
    found = []
    for name in SYSTEM_TABLES:
        try:
            open_any_table(conn, name, system=True)
            found.append(name)
        except Exception as exc:  # noqa: BLE001 - absent until first job creates it
            logger.debug("system table %s not present: %s", name, exc)
    return found


def job_id_where(fragment: str | None) -> str | None:
    """A partial-match job_id predicate, or None for blank input.

    job_id values are hex/uuid strings; quotes are dropped rather than
    escaped so the predicate can't be broken open. Partial ids match —
    pasting the 8-char prefix from a log line is enough.
    """
    fragment = (fragment or "").strip().replace("'", "")
    return f"job_id LIKE '%{fragment}%'" if fragment else None


def lead_with_job_id(cols: list[str]) -> list[str]:
    """job_id first — it's the key you filter and correlate on."""
    cols = list(cols)
    if "job_id" in cols:
        cols.insert(0, cols.pop(cols.index("job_id")))
    return cols


def detail_text(value: object) -> str:
    """The complete, untruncated rendering of one cell."""
    if value is None:
        return "(null)"
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    return str(value)


def fetch_newest_first(
    table: Any,
    cols: list[str],
    where: str | None,
    ts_col: str,
    key_col: str,
    limit: int,
) -> tuple[int, list[dict]]:
    """The newest ``limit`` rows of a system table, newest first.

    Two narrow passes through public query APIs: scan ``(ts, key)`` for every
    matching row, pick the newest keys client-side, then fetch only those rows
    in full. The key scan stays small even when the full rows carry fat
    payloads like ``error_trace``.
    """
    narrow = table.search()
    if where:
        narrow = narrow.where(where)
    index = narrow.select([ts_col, key_col]).to_list()
    index.sort(key=lambda r: (r.get(ts_col) is not None, r.get(ts_col) or 0))
    index.reverse()
    newest = index[:limit]
    if not newest:
        return len(index), []

    keys = ",".join("'{}'".format(str(r[key_col]).replace("'", "")) for r in newest)
    rows = (
        table.search()
        .where(f"{key_col} IN ({keys})")
        .select(cols)
        .limit(limit)
        .to_list()
    )
    order = {str(r[key_col]): i for i, r in enumerate(newest)}
    rows.sort(key=lambda r: order.get(str(r.get(key_col)), len(order)))
    return len(index), rows
