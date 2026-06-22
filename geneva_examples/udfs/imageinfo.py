"""Lightweight CPU UDFs: byte size + image dimensions."""

from __future__ import annotations

import os
import uuid

from geneva_examples.core.package_specs import package_spec

# Geneva remote runtime package pins (env-overridable for targeting other builds).
# geneva/lancedb/pylance track the installed versions so the workers match the
# client's locked env; the rest stay exact-pinned for reproducible worker builds.
GENEVA_PACKAGE_SPEC = package_spec("geneva")
LANCEDB_PACKAGE_SPEC = package_spec("lancedb")
PYLANCE_PACKAGE_SPEC = package_spec("pylance")
PYARROW_PACKAGE_SPEC = os.environ.get("PYARROW_PACKAGE_SPEC", "pyarrow==23.0.0")
PILLOW_PACKAGE_SPEC = os.environ.get("PILLOW_PACKAGE_SPEC", "pillow==12.2.0")

IMAGEINFO_RUNTIME_PIP = [
    GENEVA_PACKAGE_SPEC,
    LANCEDB_PACKAGE_SPEC,
    PYLANCE_PACKAGE_SPEC,
    PYARROW_PACKAGE_SPEC,
    PILLOW_PACKAGE_SPEC,
]


def build_file_size_udf(*, input_column: str, manifest: object):
    """Build a UDF returning the byte length of ``input_column``."""
    import geneva
    import pyarrow as pa

    @geneva.udf(
        data_type=pa.int64(),
        input_columns=[input_column],
        version=uuid.uuid4().hex,
        manifest=manifest,
    )
    def file_size(col: bytes) -> int:
        return len(col)

    return file_size


def build_dimensions_udf(*, input_column: str, manifest: object):
    """Build a UDF returning ``{width, height}`` of an encoded image."""
    import geneva
    import pyarrow as pa

    @geneva.udf(
        data_type=pa.struct(
            [pa.field("width", pa.int32()), pa.field("height", pa.int32())]
        ),
        input_columns=[input_column],
        version=uuid.uuid4().hex,
        manifest=manifest,
    )
    def dimensions(col: bytes) -> dict[str, int]:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(col)) as img:
            width, height = img.size
        return {"width": int(width), "height": int(height)}

    return dimensions
