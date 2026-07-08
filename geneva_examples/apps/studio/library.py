"""A local LanceDB table for saving UDFs/chunkers you're prototyping.

Backed by an on-disk LanceDB database (the ``--library`` path, default
``./udf_library``) — separate from the cloud ``db_uri`` the pipeline talks to.
One table, ``udfs``, keyed by ``name``; saving an existing name overwrites it.
This is a personal scratch library for work-in-progress functions, not a
deployment mechanism.
"""

from __future__ import annotations

import datetime
from pathlib import Path

TABLE = "udfs"


def _escape(value: str) -> str:
    """Escape single quotes for a LanceDB SQL filter literal."""
    return value.replace("'", "''")


def _db(library_path: str | Path):
    import lancedb

    path = Path(library_path).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return lancedb.connect(str(path))


def save_udf(
    library_path: str | Path, name: str, kind: str, modality: str, code: str
) -> dict:
    """Insert or overwrite the function stored under ``name``."""
    name = (name or "").strip()
    if not name:
        raise ValueError("a name is required to save")
    row = {
        "name": name,
        "kind": kind,
        "modality": modality,
        "code": code,
        "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    db = _db(library_path)
    if TABLE in db.list_tables().tables:
        # Atomic upsert-by-name: unlike a separate delete()+add(), a crash can't
        # leave the row deleted-but-not-re-added.
        (
            db.open_table(TABLE)
            .merge_insert("name")
            .when_matched_update_all()
            .when_not_matched_insert_all()
            .execute([row])
        )
    else:
        db.create_table(TABLE, data=[row])
    return row


def list_udfs(library_path: str | Path) -> list[dict]:
    """All saved functions (name/kind/modality/updated_at), newest first."""
    db = _db(library_path)
    if TABLE not in db.list_tables().tables:
        return []
    records = db.open_table(TABLE).to_pandas().to_dict("records")
    return sorted(records, key=lambda r: r.get("updated_at") or "", reverse=True)


def load_udf(library_path: str | Path, name: str) -> dict:
    """Return the stored ``{name, kind, modality, code}`` for ``name``."""
    db = _db(library_path)
    if TABLE not in db.list_tables().tables:
        raise KeyError(name)
    df = db.open_table(TABLE).to_pandas()
    match = df[df["name"] == name]
    if match.empty:
        raise KeyError(name)
    row = match.iloc[-1]
    return {
        "name": name,
        "kind": str(row["kind"]),
        "modality": str(row["modality"]),
        "code": str(row["code"]),
    }


def delete_udf(library_path: str | Path, name: str) -> None:
    """Remove the function stored under ``name`` (no-op if absent)."""
    db = _db(library_path)
    if TABLE in db.list_tables().tables:
        db.open_table(TABLE).delete(f"name = '{_escape(name)}'")
