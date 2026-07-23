"""Deliberately faulty UDF for the error-debugging demo.

Two deterministic failure modes, so the ``geneva_errors`` table has more than
one ``error_type`` to group by in the viewer:

- ``value`` divisible by ``fail_every`` raises ``ValueError`` (plays the role
  of corrupt input rows),
- ``value`` ending in 9 raises ``TimeoutError`` (plays the role of flaky I/O).

The UDF declares ``on_error=skip_on_error()``: failing rows are written as
NULL and recorded in ``geneva_errors`` while the job completes DONE — exactly
the sneaky "success with holes" shape the debugging guide teaches you to
catch and analyze.
"""

from __future__ import annotations

import uuid
from typing import Any

# The UDF body uses builtins only — no extra worker packages.
FAULTY_RUNTIME_PIP: list[str] = []


def build_faulty_score_udf(*, input_column: str, fail_every: int, manifest: Any):
    """Build a scalar UDF that fails deterministically on some rows.

    Scalar (per-row) on purpose: per-row execution is what makes geneva record
    a ``row_address`` on each error record, which is the hook for the
    retry-only-the-failed-rows workflow.
    """
    import geneva
    import pyarrow as pa
    from geneva import skip_on_error

    @geneva.udf(
        data_type=pa.float64(),
        input_columns=[input_column],
        num_cpus=1,
        version=uuid.uuid4().hex,
        manifest=manifest,
        on_error=skip_on_error(),
    )
    def faulty_score(value: int) -> float:
        if fail_every > 0 and value % fail_every == 0:
            raise ValueError(
                f"synthetic corruption: value={value} is divisible by {fail_every}"
            )
        if value % 10 == 9:
            raise TimeoutError(f"synthetic flaky I/O: timed out reading value={value}")
        return value * 1.5

    return faulty_score
