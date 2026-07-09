"""Shared fakes for the stage/ops CLI smoke tests.

The pipeline and ops CLIs are excluded from the coverage gate because their bodies
open a live Geneva/Ray connection. These fakes stand in for that cluster boundary
so a ``CliRunner`` can drive a command end-to-end — load config, connect, build a
manifest, build the UDF(s), backfill — without any network, GPU, or model weights,
guarding the wiring that the unit tests don't reach.
"""

from __future__ import annotations

import sys
import types
from typing import Any


class Job:
    """A submitted backfill job."""

    job_id = "job-smoke-1"


class Field:
    """An Arrow-style schema field (``stats`` iterates these)."""

    def __init__(self, name: str, type_: str = "string") -> None:
        self.name = name
        self.type = type_


class Schema:
    def __init__(self, names: list[str]) -> None:
        self.names = list(names)

    def __iter__(self):
        return iter(Field(n) for n in self.names)


class FakeTable:
    """Records the backfill wiring calls; supports the read chains the CLIs use."""

    def __init__(self, names: list[str] | None = None, rows: int = 0) -> None:
        self.schema = Schema(names or [])
        self._rows = rows
        self.added: dict[str, Any] = {}
        self.backfilled: list[str] = []
        self.dropped: list[str] = []
        self.adds: list[Any] = []  # record batches appended via add()

    def add(self, data: Any) -> None:
        self.adds.append(data)

    def drop_columns(self, columns: list[str]) -> None:
        self.dropped.extend(columns)

    def add_columns(self, mapping: dict[str, Any]) -> None:
        self.added.update(mapping)
        # Reflect added columns so wait_for_columns() sees them and returns.
        for name in mapping:
            if name not in self.schema.names:
                self.schema.names.append(name)

    def backfill(self, column: str, **_kwargs: Any) -> Job:
        self.backfilled.append(column)
        return Job()

    def refresh(self, **_kwargs: Any) -> None:
        pass  # materialized-view refresh (chunk CLIs)

    def checkout_latest(self) -> None:
        pass

    def count_rows(self, _filter: str | None = None) -> int:
        return self._rows

    # search().select(...).limit(n).to_list() — also search(None)/search(vec, col).
    def search(self, *_args: Any, **_kwargs: Any) -> FakeTable:
        return self

    def select(self, *_args: Any, **_kwargs: Any) -> FakeTable:
        return self

    def limit(self, *_args: Any, **_kwargs: Any) -> FakeTable:
        return self

    def to_list(self) -> list[dict[str, Any]]:
        return []


class FakeConn:
    """Opens a single table, or named tables from a mapping (``stats``).

    Also records ``create_table``/``drop_table`` for the ingest/cleanup CLIs.
    """

    def __init__(
        self,
        table: FakeTable | None = None,
        tables: dict[str, FakeTable] | None = None,
        *,
        is_remote: bool = True,
    ) -> None:
        self._table = table
        self._tables = tables or {}
        self._is_remote = is_remote
        self.created: dict[str, FakeTable] = {}
        self.dropped: list[str] = []

    def open_table(self, name: str) -> FakeTable:
        if name in self._tables:
            return self._tables[name]
        if self._table is not None:
            return self._table
        raise RuntimeError(f"table not found: {name}")

    def create_table(self, name: str, data: Any = None, **_kwargs: Any) -> FakeTable:
        table = self._table if self._table is not None else FakeTable()
        self._tables.setdefault(name, table)
        self.created[name] = table
        return table

    def drop_table(self, name: str) -> None:
        self.dropped.append(name)
        self._tables.pop(name, None)

    def create_udtf_view(
        self, name: str, source: Any = None, udtf: Any = None, **_kwargs: Any
    ) -> FakeTable:
        view = self._table if self._table is not None else FakeTable()
        self._tables.setdefault(name, view)
        self.created[name] = view
        return view

    def table_names(self) -> list[str]:
        return list(self._tables) or list(self.created)

    def is_remote(self) -> bool:
        return self._is_remote

    def local_ray_context(self):  # pragma: no cover - trivial context manager
        import contextlib

        return contextlib.nullcontext()


class FakeManifest:
    """GenevaManifest stand-in: create_pip(...).pip(...).build()."""

    @classmethod
    def create_pip(cls, _name: str) -> FakeManifest:
        return cls()

    def pip(self, _specs: list[str]) -> FakeManifest:
        return self

    def build(self) -> FakeManifest:
        return self


def install_fake_geneva(monkeypatch: Any) -> None:
    """Inject a minimal fake ``geneva`` (+ ``geneva.manifest``) into sys.modules.

    The CLIs import geneva lazily inside the command body, and the UDF factories
    decorate with ``@geneva.udf(...)``, so the fake only needs a version string and
    a pass-through ``udf`` decorator.
    """
    geneva_mod = types.ModuleType("geneva")
    geneva_mod.__version__ = "0.0.0-test"

    def udf(**_kwargs: Any):
        def decorator(fn):
            return fn

        return decorator

    geneva_mod.udf = udf  # type: ignore[attr-defined]

    manifest_mod = types.ModuleType("geneva.manifest")
    manifest_mod.GenevaManifest = FakeManifest  # type: ignore[attr-defined]
    geneva_mod.manifest = manifest_mod  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "geneva", geneva_mod)
    monkeypatch.setitem(sys.modules, "geneva.manifest", manifest_mod)
