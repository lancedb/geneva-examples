"""Helpers for waiting on Geneva/LanceDB table schema changes."""

import time


def wait_for_columns(
    conn: object, table_name: str, required: set[str], attempts: int, sleep_s: int
) -> object:
    """Reopen ``table_name`` until ``required`` columns are visible in its schema."""
    for _ in range(attempts):
        opened = conn.open_table(table_name)
        if required.issubset(set(opened.schema.names)):
            return opened
        time.sleep(sleep_s)
    raise RuntimeError(f"required columns not visible: {sorted(required)}")
