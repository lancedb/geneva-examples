"""Tests for the UDF Studio local-execution runner."""

from __future__ import annotations

from geneva_examples.apps.studio import runner


def test_run_udf_success():
    res = runner.run_udf("def transform(v):\n    return len(v)\n", [b"ab", b"abcd"])
    assert res["ok"]
    assert [r["output"] for r in res["rows"]] == ["2", "4"]
    assert all(r["error"] == "" for r in res["rows"])
    assert "2 inputs" in res["summary"]


def test_run_udf_module_level_state_runs_once():
    # Module-level code runs once; transform closes over it.
    code = "CONST = 41\n\ndef transform(v):\n    return CONST + 1\n"
    res = runner.run_udf(code, [b"x", b"y"])
    assert res["ok"]
    assert [r["output"] for r in res["rows"]] == ["42", "42"]


def test_run_udf_setup_is_called():
    code = (
        "STATE = []\n"
        "def setup():\n    STATE.append('ready')\n"
        "def transform(v):\n    return STATE[0]\n"
    )
    res = runner.run_udf(code, [b"x"])
    assert res["ok"]
    assert res["rows"][0]["output"] == "ready"


def test_run_udf_missing_transform():
    res = runner.run_udf("x = 1\n", [b"a"])
    assert not res["ok"]
    assert "transform" in res["error"]


def test_run_udf_per_row_error_is_isolated():
    code = "def transform(v):\n    if v == b'bad':\n        raise ValueError('boom')\n    return len(v)\n"
    res = runner.run_udf(code, [b"ok", b"bad", b"fine"])
    assert res["ok"]
    assert res["rows"][0]["error"] == ""
    assert "ValueError: boom" in res["rows"][1]["error"]
    assert res["rows"][2]["error"] == ""
    assert "1 error(s)" in res["summary"]


def test_run_udf_empty_values():
    res = runner.run_udf("def transform(v):\n    return v\n", [])
    assert not res["ok"]
    assert "samples" in res["summary"].lower()


def test_run_udf_compile_error_surfaces_traceback():
    res = runner.run_udf("def transform(v)\n    return v\n", [b"a"])  # missing colon
    assert not res["ok"]
    assert "SyntaxError" in res["error"]


def test_run_chunker_collects_rows():
    code = "def chunk(v):\n    for i in range(v):\n        yield {'i': i}\n"
    res = runner.run_chunker(code, [2, 3])
    assert res["ok"]
    assert len(res["rows"]) == 5
    assert res["rows"][0] == {"input": 0, "i": "0"}
    assert "5 output row(s)" in res["summary"]


def test_run_chunker_missing_chunk():
    res = runner.run_chunker("def transform(v):\n    return v\n", [1])
    assert not res["ok"]
    assert "chunk" in res["error"]


def test_run_chunker_empty_values():
    res = runner.run_chunker("def chunk(v):\n    yield {}\n", [])
    assert not res["ok"]
    assert "samples" in res["summary"].lower()


def test_run_chunker_compile_error():
    res = runner.run_chunker("def chunk(v)\n    yield 1\n", [1])  # missing colon
    assert not res["ok"]
    assert "SyntaxError" in res["error"]


def test_run_chunker_per_input_error_isolated():
    code = "def chunk(v):\n    if v == 0:\n        raise ValueError('bad')\n    yield {'v': v}\n"
    res = runner.run_chunker(code, [0, 1])
    assert res["ok"]
    assert any("error" in r and "ValueError" in r["error"] for r in res["rows"])
    assert any(r.get("v") == "1" for r in res["rows"])


def test_run_chunker_non_dict_yield():
    res = runner.run_chunker("def chunk(v):\n    yield v * 2\n", [3])
    assert res["ok"]
    assert res["rows"][0]["value"] == "6"


class _BadList:
    def tolist(self):
        raise RuntimeError("nope")

    def __repr__(self):
        return "BadList()"


def test_short_tolist_exception_falls_back_to_repr():
    assert runner._short(_BadList()) == "BadList()"


def test_run_dispatch_by_kind():
    udf_code = "def transform(v):\n    return v\n"
    chunk_code = "def chunk(v):\n    yield {'v': v}\n"
    assert runner.run("udf", udf_code, [1])["ok"]
    assert runner.run("chunker", chunk_code, [1])["ok"]


class _FakeArray:
    def tolist(self):
        return [1, 2, 3]


def test_short_formatting():
    assert runner._short(None) == ""
    assert runner._short(b"abcd") == "<4 bytes>"
    assert runner._short(_FakeArray()) == "[1, 2, 3] (len 3)"
    assert runner._short(list(range(10))).endswith("(len 10)")
    assert runner._short("x" * 500).endswith("…")
    assert runner._short("short") == "short"
