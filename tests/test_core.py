"""Tests for core helpers: memory sizing, retry, schema-wait."""

from __future__ import annotations

import logging
import os

import pytest

from geneva_examples.core import common
from geneva_examples.core.config import Config
from geneva_examples.core.utils import retry, tables


def test_setup_logging_runs():
    common.setup_logging("DEBUG")  # configures the root logger without raising


def test_setup_logging_quiets_noisy_loggers(monkeypatch):
    monkeypatch.delenv("LANCE_LOG", raising=False)
    common.setup_logging("INFO")
    assert os.environ.get("LANCE_LOG") == "warn"
    assert logging.getLogger("geneva").level == logging.WARNING
    assert logging.getLogger("ray").level == logging.WARNING
    assert logging.getLogger("geneva_examples").level == logging.INFO


def test_setup_logging_debug_keeps_verbose(monkeypatch):
    monkeypatch.delenv("LANCE_LOG", raising=False)
    common.setup_logging("DEBUG")
    assert "LANCE_LOG" not in os.environ  # not forced in debug mode
    assert logging.getLogger("geneva_examples").level == logging.DEBUG


def test_format_sample_empty():
    assert common.format_sample([]) == "(no rows)"


def test_format_sample_summarizes_values():
    rows = [
        {
            "id": "a",
            "emb": [0.1] * 512,
            "raw": b"xyz",
            "dim": {"width": 10, "height": 20},
            "score": 1.23456,
        }
    ]
    out = common.format_sample(rows)
    assert "id" in out and "emb" in out
    assert "[512 floats]" in out  # embedding summarized
    assert "<3 B>" in out  # bytes summarized
    assert "width=10 height=20" in out  # struct flattened
    assert "1.235" in out  # float rounded


def test_format_sample_respects_column_order():
    out = common.format_sample([{"a": 1, "b": 2}], columns=["b", "a"])
    header = out.splitlines()[0]
    assert header.index("b") < header.index("a")


def test_memory_request_bytes_normal():
    assert common.memory_request_bytes(1) == 1024**3


def test_memory_request_bytes_caps_to_32bit(caplog):
    capped = common.memory_request_bytes(4)  # 4 GiB > 2**31-1
    assert capped == 2**31 - 1


# --- mode-aware helpers ------------------------------------------------------


def test_build_manifest_is_none_locally():
    assert common.build_manifest(Config(mode="local"), "x", ["pkg==1"]) is None


def test_resolve_resources_clamps_locally(monkeypatch):
    # Ample RAM: the memory reservation is left at the requested 1 GiB.
    monkeypatch.setattr(common, "total_ram_bytes", lambda: 64 * 1024**3)
    cfg = Config(mode="local")
    num_cpus, num_gpus, mem = common.resolve_resources(
        cfg, num_cpus=64.0, num_gpus=1.0, memory_gib=1
    )
    assert num_gpus == 0
    assert num_cpus <= (__import__("os").cpu_count() or 1)
    assert mem == 1024**3


def test_resolve_resources_caps_memory_on_small_box(monkeypatch):
    # 2 GB box: the advisory Ray memory reservation is capped to ~25% of RAM so
    # local Ray can still schedule the task.
    monkeypatch.setattr(common, "total_ram_bytes", lambda: 2 * 1024**3)
    cfg = Config(mode="local")
    _cpus, _gpus, mem = common.resolve_resources(
        cfg, num_cpus=8.0, num_gpus=1.0, memory_gib=1
    )
    assert mem == int(2 * 1024**3 * 0.25)  # 512 MiB, below the 1 GiB request


def test_resolve_resources_memory_floor(monkeypatch):
    # Even on a tiny box the reservation never drops below the 256 MiB floor.
    monkeypatch.setattr(common, "total_ram_bytes", lambda: 512 * 1024**2)
    cfg = Config(mode="local")
    _cpus, _gpus, mem = common.resolve_resources(
        cfg, num_cpus=2.0, num_gpus=None, memory_gib=1
    )
    assert mem == 256 * 1024**2


def test_resolve_resources_passthrough_enterprise():
    cfg = Config(mode="enterprise")
    num_cpus, num_gpus, mem = common.resolve_resources(
        cfg, num_cpus=8.0, num_gpus=0.5, memory_gib=1
    )
    assert (num_cpus, num_gpus, mem) == (8.0, 0.5, 1024**3)


def test_local_or_picks_by_mode():
    assert common.local_or(Config(mode="local"), 1, 99) == 1
    assert common.local_or(Config(mode="enterprise"), 1, 99) == 99


def test_local_concurrency_caps_leaving_a_core(monkeypatch):
    monkeypatch.setattr(common.os, "cpu_count", lambda: 4)
    assert common.local_concurrency(32) == 3  # cores - 1
    assert common.local_concurrency(2) == 2  # already below the cap


def test_local_concurrency_floor(monkeypatch):
    monkeypatch.setattr(common.os, "cpu_count", lambda: 1)
    assert common.local_concurrency(32) == 1  # never drops below 1


def test_runtime_session_nullcontext_in_enterprise():
    # Enterprise mode never touches the connection; a no-op context is returned.
    ctx = common.runtime_session(object(), Config(mode="enterprise"))
    with ctx:
        pass


def test_runtime_session_local_disables_log_forwarding(monkeypatch):
    # Local mode provisions Ray with worker-log forwarding OFF (clean console).
    import contextlib

    import geneva.runners.ray._mgr as mgr

    captured: dict = {}

    def fake_ray_cluster(**kwargs):
        captured.update(kwargs)
        return contextlib.nullcontext()

    monkeypatch.setattr(mgr, "ray_cluster", fake_ray_cluster)
    common.setup_logging("INFO")
    with common.runtime_session(object(), Config(mode="local")):
        pass
    assert captured["local"] is True
    assert captured["log_to_driver"] is False


def test_runtime_session_local_verbose_in_debug(monkeypatch):
    import contextlib

    import geneva.runners.ray._mgr as mgr

    captured: dict = {}
    monkeypatch.setattr(
        mgr,
        "ray_cluster",
        lambda **kw: (captured.update(kw), contextlib.nullcontext())[1],
    )
    common.setup_logging("DEBUG")
    with common.runtime_session(object(), Config(mode="local")):
        pass
    assert captured["log_to_driver"] is True
    common.setup_logging("INFO")  # restore for other tests


def test_runtime_session_falls_back_to_public_api(monkeypatch):
    """If geneva's internal ray_cluster goes away, degrade — don't crash.

    The private import is the quiet-logging path; a geneva pin that renames it
    must still leave local runs working via the public context manager.
    """
    import contextlib

    import geneva.runners.ray._mgr as mgr

    def _boom(**_kwargs):
        raise AttributeError("ray_cluster moved")

    monkeypatch.setattr(mgr, "ray_cluster", _boom)

    used: dict = {}

    class _Conn:
        def local_ray_context(self):
            used["public"] = True
            return contextlib.nullcontext()

    with common.runtime_session(_Conn(), Config(mode="local")):
        pass

    assert used == {"public": True}


def test_resolve_resources_local_without_ram_reading(monkeypatch):
    """A platform where total RAM is unreadable still clamps CPU/GPU."""
    monkeypatch.setattr(common, "total_ram_bytes", lambda: None)

    cpus, gpus, memory = common.resolve_resources(
        Config(mode="local"), num_cpus=999, num_gpus=1, memory_gib=8
    )

    assert gpus == 0  # local Ray has no GPU to schedule against
    assert cpus <= (os.cpu_count() or 1)
    assert memory == common.memory_request_bytes(8)  # left as requested


def test_connect_local_uses_path(monkeypatch):
    captured = {}

    class _FakeGeneva:
        def connect(self, **kwargs):
            captured.update(kwargs)
            return "conn"

    import sys

    monkeypatch.setitem(sys.modules, "geneva", _FakeGeneva())
    common.connect(Config(mode="local", local_db_path="/tmp/xyzdb"))
    from pathlib import Path

    assert captured["uri"] == Path("/tmp/xyzdb")
    assert "api_key" not in captured  # no cloud creds in local mode


def test_connect_enterprise_passes_cloud_credentials(monkeypatch):
    captured = {}

    class _FakeGeneva:
        def connect(self, **kwargs):
            captured.update(kwargs)
            return "conn"

    import sys

    monkeypatch.setitem(sys.modules, "geneva", _FakeGeneva())
    common.connect(
        Config(
            mode="enterprise",
            db_uri="db://prod",
            geneva_host="http://host",
            lancedb_api_key="key",
            lancedb_region="us-east-1",
        )
    )

    # The uri goes through as the db:// URI, not a Path — that distinction is
    # what selects geneva's remote connection over an on-disk database.
    assert captured["uri"] == "db://prod"
    assert captured["host_override"] == "http://host"
    assert captured["api_key"] == "key"
    assert captured["region"] == "us-east-1"


def test_build_manifest_builds_a_pinned_manifest_in_enterprise(monkeypatch):
    import sys
    import types as _types

    built: dict = {}

    class _Manifest:
        @classmethod
        def create_pip(cls, name):
            built["name"] = name
            return cls()

        def pip(self, specs):
            built["pip"] = specs
            return self

        def build(self):
            built["built"] = True
            return "manifest"

    module = _types.ModuleType("geneva.manifest")
    module.GenevaManifest = _Manifest
    monkeypatch.setitem(sys.modules, "geneva.manifest", module)

    out = common.build_manifest(Config(mode="enterprise"), "vid", ["pkg==1"])

    assert out == "manifest"
    assert built["pip"] == ["pkg==1"]
    assert built["built"] is True
    # the prefix gets a random suffix so repeat runs don't collide
    assert built["name"].startswith("vid-") and len(built["name"]) > len("vid-")


def test_retry_io_returns_on_first_success():
    assert retry.retry_io("op", lambda: 42) == 42


def test_retry_io_without_jitter_sleeps_exact_backoff(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(retry.time, "sleep", slept.append)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")
        return "ok"

    # jitter=0 -> the backoff is exactly sleep_s * attempt, no randomization
    assert retry.retry_io("op", flaky, attempts=5, sleep_s=2, jitter=0) == "ok"
    assert slept == [2, 4]


def test_retry_io_with_zero_attempts_returns_none():
    # Degenerate but defined: the loop body never runs, nothing is raised.
    assert retry.retry_io("op", lambda: 42, attempts=0) is None


def test_retry_io_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(retry.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")
        return "ok"

    assert retry.retry_io("op", flaky, attempts=5, sleep_s=0.01) == "ok"
    assert calls["n"] == 3


def test_retry_io_raises_after_exhausting(monkeypatch):
    monkeypatch.setattr(retry.time, "sleep", lambda _s: None)

    def always_fails():
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        retry.retry_io("op", always_fails, attempts=3, sleep_s=0.01)


def test_retry_io_does_not_retry_unlisted_exception(monkeypatch):
    sleeps = []
    monkeypatch.setattr(retry.time, "sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    def fails_with_type_error():
        calls["n"] += 1
        raise TypeError("not transient")

    # Only ConnectionError is retryable, so TypeError propagates on the first try.
    with pytest.raises(TypeError, match="not transient"):
        retry.retry_io(
            "op", fails_with_type_error, attempts=5, retry_on=ConnectionError
        )
    assert calls["n"] == 1
    assert sleeps == []


class _Schema:
    def __init__(self, names):
        self.names = names


class _Table:
    def __init__(self, names):
        self.schema = _Schema(names)


class _Conn:
    """Returns a table whose columns appear only on the Nth open_table call."""

    def __init__(self, names_per_call):
        self._names_per_call = list(names_per_call)
        self.calls = 0

    def open_table(self, _name):
        names = self._names_per_call[min(self.calls, len(self._names_per_call) - 1)]
        self.calls += 1
        return _Table(names)


def test_wait_for_columns_returns_when_present(monkeypatch):
    monkeypatch.setattr(tables.time, "sleep", lambda _s: None)
    conn = _Conn([[], ["a", "b"]])  # missing first, present second
    opened = tables.wait_for_columns(
        conn=conn, table_name="t", required={"a"}, attempts=5, sleep_s=0
    )
    assert "a" in opened.schema.names
    assert conn.calls == 2


def test_wait_for_columns_times_out(monkeypatch):
    monkeypatch.setattr(tables.time, "sleep", lambda _s: None)
    conn = _Conn([[]])  # never has the column
    with pytest.raises(RuntimeError, match="required columns not visible"):
        tables.wait_for_columns(
            conn=conn, table_name="t", required={"z"}, attempts=3, sleep_s=0
        )


def test_format_cell_bounds_long_and_multiline_values():
    assert common.format_cell("Traceback:\n  boom") == "Traceback: …"
    out = common.format_cell("x" * 200)
    assert len(out) == 120
    assert out.endswith("…")
    assert common.format_cell("short") == "short"


def test_format_cell_summarizes_opaque_values():
    """The grid must stay one line per cell whatever geneva hands back."""
    assert common.format_cell(None) == ""
    assert common.format_cell(b"\x00\x01\x02") == "<3 B>"
    assert common.format_cell(3.14159) == "3.142"
    assert common.format_cell([0.1] * 512) == "[512 floats]"
    assert common.format_cell(["a"] * 20) == "[20 items]"  # long, not numeric
    assert common.format_cell([1, 2, 3]) == "[1, 2, 3]"  # short enough to show
    assert common.format_cell({"w": 640, "h": 480}) == "w=640 h=480"
