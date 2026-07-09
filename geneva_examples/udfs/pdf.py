"""Geneva UDF factories for PDF page/text-chunk extraction.

Unlike the other modules here, these factories don't define new UDF bodies —
they **reuse the pre-built document UDFs Geneva ships** in
``geneva.udfs.document`` (see
``geneva/udfs/document/pdf_embedding.py`` in the installed package):

  - :func:`build_extract_pages_udf` wraps ``extract_pages`` — decode a PDF's
    bytes with ``pypdf`` and emit one ``{page_number, text}`` per page.
  - :func:`build_chunk_pages_udf` wraps ``chunk_pages`` — split each page's text
    into overlapping windows with LangChain's ``RecursiveCharacterTextSplitter``
    (``CHUNK_SIZE=2048``, ``CHUNK_OVERLAP=200``), emitting one
    ``{page_number, chunk_id, chunk}`` per chunk.

Both are scalar ``@geneva.udf``s (one PDF row -> one nested-list value), so they
chain through the same single-column backfill helper the image stages use. Their
input columns are inferred from the parameter names: ``extract_pages`` binds to a
``pdf_bytes`` column and ``chunk_pages`` to a ``pages`` column — so the ingest
table must expose ``pdf_bytes`` and the first backfill must output ``pages``.

The pre-built UDFs are declared with ``manifest=None`` (they resolve the
deployment-default manifest server-side). We ``attrs.evolve`` a copy with this
repo's pinned-pip manifest so the workers install the exact versions the client
locked — matching how the ``build_*`` factories in the sibling modules work.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from geneva_examples.core.package_specs import package_spec

# Geneva remote runtime package pins (env-overridable for targeting other builds).
# geneva/lancedb/pylance track the installed versions so the workers match the
# client's locked env; the rest stay exact-pinned for reproducible worker builds.
# pypdf + langchain-text-splitters back extract_pages/chunk_pages respectively.
GENEVA_PACKAGE_SPEC = package_spec("geneva")
LANCEDB_PACKAGE_SPEC = package_spec("lancedb")
PYLANCE_PACKAGE_SPEC = package_spec("pylance")
PYARROW_PACKAGE_SPEC = os.environ.get("PYARROW_PACKAGE_SPEC", "pyarrow==23.0.0")
PYPDF_PACKAGE_SPEC = os.environ.get("PYPDF_PACKAGE_SPEC", "pypdf>=5,<6")
LANGCHAIN_TEXT_SPLITTERS_PACKAGE_SPEC = os.environ.get(
    "LANGCHAIN_TEXT_SPLITTERS_PACKAGE_SPEC", "langchain-text-splitters>=0.3,<0.4"
)

PDF_RUNTIME_PIP = [
    GENEVA_PACKAGE_SPEC,
    LANCEDB_PACKAGE_SPEC,
    PYLANCE_PACKAGE_SPEC,
    PYARROW_PACKAGE_SPEC,
    PYPDF_PACKAGE_SPEC,
    LANGCHAIN_TEXT_SPLITTERS_PACKAGE_SPEC,
]


def build_extract_pages_udf(*, manifest: Any):
    """Build the ``extract_pages`` UDF (``pdf_bytes`` -> per-page text).

    Reuses ``geneva.udfs.document.extract_pages`` with this repo's manifest and a
    fresh ``version`` so re-runs re-materialize. Output type:
    ``list<struct{page_number:int32, text}>``; input column: ``pdf_bytes``.
    """
    import attrs
    from geneva.udfs.document import extract_pages

    return attrs.evolve(extract_pages, manifest=manifest, version=uuid.uuid4().hex)


def build_chunk_pages_udf(*, manifest: Any):
    """Build the ``chunk_pages`` UDF (``pages`` -> overlapping text chunks).

    Reuses ``geneva.udfs.document.chunk_pages`` with this repo's manifest and a
    fresh ``version``. Output type:
    ``list<struct{page_number:int32, chunk_id:int32, chunk}>``; input column:
    ``pages`` (produced by :func:`build_extract_pages_udf`).
    """
    import attrs
    from geneva.udfs.document import chunk_pages

    return attrs.evolve(chunk_pages, manifest=manifest, version=uuid.uuid4().hex)
