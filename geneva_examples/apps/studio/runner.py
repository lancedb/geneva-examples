"""Run an edited UDF/chunker locally (on the driver) against sample values.

The Studio's execution contract is deliberately small and decoupled from the
``@geneva.udf`` / ``@geneva.chunker`` decorators so prototyping needs no Ray,
GPU, or cluster — just this process:

- **UDF mode** — the code must define ``transform(value)``: one input element in,
  one output out. Any module-level code (e.g. loading a model) runs once before
  ``transform`` is mapped over the samples. An optional ``setup()`` is called
  once after exec if present.
- **Chunker mode** — the code must define ``chunk(value)``: a generator yielding
  one ``dict`` per output row.

Each ``value`` is whatever the sampler produced for the chosen modality: raw
``bytes`` for image/video/audio, or a ``str`` for text. Errors are caught
per-row and returned (never raised) so one bad sample doesn't sink the run.
"""

from __future__ import annotations

import time
import traceback
from typing import Any


def _short(value: Any, limit: int = 200) -> str:
    """Compact, display-safe representation of an output value."""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    # numpy arrays / torch tensors / anything list-like with .tolist()
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        try:
            value = tolist()
        except Exception:  # noqa: BLE001 (best-effort preview only)
            pass
    if isinstance(value, (list, tuple)):
        n = len(value)
        head = ", ".join(repr(x) for x in value[:6])
        return f"[{head}{', …' if n > 6 else ''}] (len {n})"
    text = repr(value) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[:limit] + "…"


def _exec_user_code(code: str) -> dict:
    """Execute the editor's code in a fresh namespace; run ``setup()`` if defined."""
    ns: dict[str, Any] = {}
    exec(compile(code, "<udf-studio>", "exec"), ns)
    setup = ns.get("setup")
    if callable(setup):
        setup()
    return ns


def run_udf(code: str, values: list) -> dict:
    """Map ``transform(value)`` over ``values``; return per-row outputs + summary."""
    if not values:
        return {
            "ok": False,
            "error": "",
            "rows": [],
            "summary": "Load some samples first.",
        }
    try:
        ns = _exec_user_code(code)
    except Exception:  # noqa: BLE001 (surface to the UI, don't crash the app)
        return {
            "ok": False,
            "error": traceback.format_exc(),
            "rows": [],
            "summary": "❌ code/setup failed",
        }

    fn = ns.get("transform")
    if not callable(fn):
        return {
            "ok": False,
            "error": "Your code must define a function `transform(value)`.",
            "rows": [],
            "summary": "❌ no `transform` defined",
        }

    rows, out_type, n_err = [], None, 0
    t0 = time.perf_counter()
    for i, v in enumerate(values):
        try:
            out = fn(v)
            if out_type is None and out is not None:
                out_type = type(out).__name__
            rows.append({"row": i, "output": _short(out), "error": ""})
        except Exception as e:  # noqa: BLE001 (per-row; keep going)
            n_err += 1
            rows.append({"row": i, "output": "", "error": f"{type(e).__name__}: {e}"})
    dt = (time.perf_counter() - t0) * 1000

    summary = (
        f"**{len(values)} inputs → {len(values) - n_err} ok, {n_err} error(s)** · "
        f"output type: `{out_type or 'n/a'}` · {dt:.0f} ms"
    )
    return {"ok": True, "error": "", "rows": rows, "summary": summary}


def run_chunker(code: str, values: list) -> dict:
    """Iterate ``chunk(value)`` over ``values``; collect all yielded rows."""
    if not values:
        return {
            "ok": False,
            "error": "",
            "rows": [],
            "summary": "Load some samples first.",
        }
    try:
        ns = _exec_user_code(code)
    except Exception:  # noqa: BLE001
        return {
            "ok": False,
            "error": traceback.format_exc(),
            "rows": [],
            "summary": "❌ code/setup failed",
        }

    fn = ns.get("chunk")
    if not callable(fn):
        return {
            "ok": False,
            "error": "Your code must define a generator function `chunk(value)`.",
            "rows": [],
            "summary": "❌ no `chunk` defined",
        }

    rows, n_err = [], 0
    t0 = time.perf_counter()
    for i, v in enumerate(values):
        try:
            for item in fn(v):
                row = {"input": i}
                if isinstance(item, dict):
                    row.update({k: _short(val) for k, val in item.items()})
                else:
                    row["value"] = _short(item)
                rows.append(row)
        except Exception as e:  # noqa: BLE001 (per-input; keep going)
            n_err += 1
            rows.append({"input": i, "error": f"{type(e).__name__}: {e}"})
    dt = (time.perf_counter() - t0) * 1000

    summary = (
        f"**{len(values)} inputs → {len(rows)} output row(s)** · "
        f"{n_err} input(s) errored · {dt:.0f} ms"
    )
    return {"ok": True, "error": "", "rows": rows, "summary": summary}


def run(kind: str, code: str, values: list) -> dict:
    """Dispatch to :func:`run_udf` or :func:`run_chunker` by ``kind``."""
    return run_chunker(code, values) if kind == "chunker" else run_udf(code, values)
