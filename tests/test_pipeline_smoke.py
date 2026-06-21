"""End-to-end smoke test for a stage CLI.

The stage CLIs are excluded from the coverage gate because their bodies open a
live Geneva/Ray connection. Their unit-tested pieces (the ``backfill_column``
runner, the UDF manifests) are covered elsewhere, but nothing exercises the
*wiring* that glues them together: load config -> connect -> build manifest ->
build UDFs -> backfill each column. This test drives the simplest stage
(``lightweight``, CPU-only) through ``typer``'s ``CliRunner`` with the cluster
boundary mocked out, so a regression in that glue fails fast without needing a
cluster, GPU, or network.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from typer.testing import CliRunner

from geneva_examples.pipeline.stages import lightweight


class _Schema:
    def __init__(self, names: list[str]) -> None:
        self.names = names


class _Job:
    job_id = "job-smoke-1"


class _Table:
    """A fake table that records the backfill wiring calls the stage makes."""

    def __init__(self, names: list[str]) -> None:
        self.schema = _Schema(names)
        self.added: dict[str, Any] = {}
        self.backfilled: list[str] = []
        self.dropped: list[str] = []

    def drop_columns(self, columns: list[str]) -> None:
        self.dropped.extend(columns)

    def add_columns(self, mapping: dict[str, Any]) -> None:
        self.added.update(mapping)

    def backfill(self, column: str, **_kwargs: Any) -> _Job:
        self.backfilled.append(column)
        return _Job()

    def checkout_latest(self) -> None:
        pass

    def count_rows(self, _filter: str | None = None) -> int:
        return 0

    # The stage logs a small sample at the end via table.search().select(...).
    def search(self) -> _Table:
        return self

    def select(self, _columns: list[str]) -> _Table:
        return self

    def limit(self, _n: int) -> _Table:
        return self

    def to_list(self) -> list[dict[str, Any]]:
        return []


class _Conn:
    def __init__(self, table: _Table) -> None:
        self._table = table

    def open_table(self, _name: str) -> _Table:
        return self._table


class _Manifest:
    """Fake GenevaManifest builder: create_pip(...).pip(...).build()."""

    @classmethod
    def create_pip(cls, _name: str) -> _Manifest:
        return cls()

    def pip(self, _specs: list[str]) -> _Manifest:
        return self

    def build(self) -> _Manifest:
        return self


@pytest.fixture
def fake_geneva(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject a minimal fake ``geneva`` (+ ``geneva.manifest``) into sys.modules.

    The stage imports geneva lazily inside the command body, and the imageinfo
    UDF factories decorate with ``@geneva.udf(...)``, so the fake only needs a
    version string and a pass-through ``udf`` decorator.
    """
    geneva_mod = types.ModuleType("geneva")
    geneva_mod.__version__ = "0.0.0-test"

    def udf(**_kwargs: Any):
        def decorator(fn):
            return fn

        return decorator

    geneva_mod.udf = udf  # type: ignore[attr-defined]

    manifest_mod = types.ModuleType("geneva.manifest")
    manifest_mod.GenevaManifest = _Manifest  # type: ignore[attr-defined]
    geneva_mod.manifest = manifest_mod  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "geneva", geneva_mod)
    monkeypatch.setitem(sys.modules, "geneva.manifest", manifest_mod)


def test_lightweight_stage_wires_both_columns(
    monkeypatch: pytest.MonkeyPatch, fake_geneva: None
) -> None:
    # The reopened table must already expose both columns so wait_for_columns
    # returns immediately instead of looping.
    table = _Table(["image_id", "label", "file_size", "dimensions"])

    cfg = types.SimpleNamespace(db_uri="db://test", table_name="images")
    monkeypatch.setattr(lightweight, "load_config", lambda _config: cfg)
    monkeypatch.setattr(lightweight, "connect", lambda _cfg: _Conn(table))

    result = CliRunner().invoke(lightweight.app, ["--schema-wait-sleep-s", "0"])

    assert result.exit_code == 0, result.output
    # Both feature columns were added and backfilled through the shared runner.
    assert set(table.added) == {"file_size", "dimensions"}
    assert table.backfilled == ["file_size", "dimensions"]
