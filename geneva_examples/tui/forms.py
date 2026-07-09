"""Pure helpers for the TUI parameter form.

Kept free of Textual imports so the value coercion is unit-testable without a
running app.
"""

from __future__ import annotations

from typing import Any

from geneva_examples.core.spec import Param


def field_id(param: Param) -> str:
    """Stable widget id for a param's input field."""
    return f"param-{param.name.replace('_', '-')}"


def coerce(param: Param, raw: Any) -> Any:
    """Coerce a raw widget value to the param's type.

    Empty strings fall back to the param default (so a blank field means "use the
    default"). Bools come straight from a Switch; choices/str pass through.
    """
    if param.type is bool:
        return bool(raw)
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return param.default
    if param.type is int:
        return int(raw)
    if param.type is float:
        return float(raw)
    return raw


def initial_text(param: Param) -> str:
    """Initial text for a str/int/float input (empty when the default is None)."""
    if param.default is None:
        return ""
    return str(param.default)
