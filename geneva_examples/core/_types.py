"""Structural type aliases for the Geneva/LanceDB objects the helpers touch.

These ``Protocol``s capture only the attributes/methods the example code calls,
so the shared helpers can carry precise signatures without importing geneva's
heavyweight, beta-pinned runtime types. They are referenced only from annotations
(the modules use ``from __future__ import annotations``) and imported under
``TYPE_CHECKING``, so there is no import-time cost on the driver or remote workers.
"""

from __future__ import annotations

from typing import Protocol


class SchemaLike(Protocol):
    """A table schema exposing its column ``names`` (Arrow-style)."""

    names: list[str]


class JobLike(Protocol):
    """A submitted Geneva backfill job, identified by ``job_id``."""

    job_id: str


class TableLike(Protocol):
    """The subset of a Geneva/LanceDB table the stage helpers use."""

    schema: SchemaLike

    def drop_columns(self, columns: list[str]) -> object: ...

    def add_columns(self, mapping: dict[str, object]) -> object: ...

    def backfill(self, column: str, **kwargs: object) -> JobLike: ...

    def checkout_latest(self) -> object: ...

    def count_rows(self, filter: str | None = ...) -> int: ...


class ConnectionLike(Protocol):
    """The subset of a Geneva connection the stage helpers use."""

    def open_table(self, name: str) -> TableLike: ...
