"""UDF proving the ffmpeg CLI is present on the remote worker via conda.

Only meaningful in enterprise mode: build_manifest() (see
geneva_examples/core/common.py) folds COMMON_CONDA_DEPENDENCIES -- including
ffmpeg -- into every conda manifest it builds, regardless of the caller's own
pip list. This UDF shells out to the real `ffmpeg` binary (not a bundled
library like PyAV's) so a passing backfill is direct evidence that the conda
env actually resolved on the cluster, not just that build_manifest() built an
object without erroring.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from typing import Any

from geneva_examples.core.package_specs import package_spec

# Geneva remote runtime package pins (env-overridable for targeting other
# builds), matching every other *_RUNTIME_PIP in this repo. geneva/lancedb/
# pylance must track the installed versions (not just geneva): a mismatched
# lancedb/pylance raises "Can't instantiate abstract class Table without an
# implementation for abstract method ..." at backfill setup, since the
# concrete Table impl and the ABC it satisfies have to come from compatible
# builds. ffmpeg itself comes from COMMON_CONDA_DEPENDENCIES in
# build_manifest(), not from this list -- this is only the pip side of the
# conda env.
GENEVA_PACKAGE_SPEC = package_spec("geneva")
LANCEDB_PACKAGE_SPEC = package_spec("lancedb")
PYLANCE_PACKAGE_SPEC = package_spec("pylance")
PYARROW_PACKAGE_SPEC = os.environ.get("PYARROW_PACKAGE_SPEC", "pyarrow==23.0.1")

FFMPEG_PROBE_RUNTIME_PIP = [
    GENEVA_PACKAGE_SPEC,
    LANCEDB_PACKAGE_SPEC,
    PYLANCE_PACKAGE_SPEC,
    PYARROW_PACKAGE_SPEC,
]


def build_ffmpeg_probe_udf(*, input_column: str, manifest: Any):
    """Build a scalar UDF returning `ffmpeg -version`'s first line, or the error."""

    import geneva
    import pyarrow as pa

    @geneva.udf(
        data_type=pa.string(),
        input_columns=[input_column],
        num_cpus=1,
        version=uuid.uuid4().hex,
        manifest=manifest,
    )
    def ffmpeg_version(_id: int) -> str:
        try:
            out = subprocess.run(
                ["ffmpeg", "-version"],  # noqa: S607 - PATH lookup is the point of this probe
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            return out.stdout.splitlines()[0]
        except (OSError, subprocess.SubprocessError) as exc:
            return f"ffmpeg unavailable: {exc}"

    return ffmpeg_version
