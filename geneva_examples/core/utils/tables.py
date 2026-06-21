"""Helpers for waiting on Geneva/LanceDB table schema changes."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from geneva_examples.core._types import ConnectionLike, TableLike


def wait_for_columns(
    conn: ConnectionLike,
    table_name: str,
    required: set[str],
    attempts: int,
    sleep_s: int,
) -> TableLike:
    """Reopen ``table_name`` until ``required`` columns are visible in its schema."""
    for _ in range(attempts):
        opened = conn.open_table(table_name)
        if required.issubset(set(opened.schema.names)):
            return opened
        time.sleep(sleep_s)
    raise RuntimeError(f"required columns not visible: {sorted(required)}")
