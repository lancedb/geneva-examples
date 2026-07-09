"""Tests for the TUI's pure form helpers (value coercion)."""

from __future__ import annotations

from geneva_examples.core.spec import Param
from geneva_examples.tui.forms import coerce, field_id, initial_text


def test_coerce_bool():
    assert coerce(Param("f", bool, False, "h"), True) is True
    assert coerce(Param("f", bool, True, "h"), False) is False


def test_coerce_numbers():
    assert coerce(Param("n", int, 1, "h"), "7") == 7
    assert coerce(Param("x", float, 1.0, "h"), "2.5") == 2.5


def test_coerce_empty_falls_back_to_default():
    assert coerce(Param("n", int, 5, "h"), "") == 5
    assert coerce(Param("g", float, None, "h"), "") is None


def test_coerce_str_passthrough():
    assert coerce(Param("s", str, "a", "h"), "b") == "b"


def test_field_id_kebab():
    assert field_id(Param("num_gpus", float, None, "h")) == "param-num-gpus"


def test_initial_text():
    assert initial_text(Param("n", int, 5, "h")) == "5"
    assert initial_text(Param("g", float, None, "h")) == ""
