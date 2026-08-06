"""Generate the committed reference docs from the spec registry.

``uv run python -m geneva_examples.docs_gen`` rewrites the generated files;
``--check`` re-renders them in memory and fails if the committed copies are
stale, missing, or if an unexpected file sits in the machine-owned
``docs/reference/cli/`` directory. ``make docs`` / ``make docs-check`` wrap the
two modes, and CI runs the check.

What is generated, and from what:

- ``docs/reference/cli/<example>.md`` + ``index.md`` — rendered from the spec
  registry (``geneva_examples/examples/__init__.py``) through
  :func:`geneva_examples.core.spec.build_command`, so the pages are
  definitionally identical to each command's ``--help``.
- ``docs/reference/worker-runtime-pins.md`` — the ``*_RUNTIME_PIP`` manifest
  contents and ``*_PACKAGE_SPEC`` env overrides, read from the manifest
  modules (:data:`geneva_examples.docs_gen.pins.MANIFEST_MODULES`).
- ``llms.txt`` — the LLM-facing index, rendered from the curated link
  manifest in :mod:`geneva_examples.docs_gen.llms`.
- ``llms-full.txt`` — the whole docs corpus concatenated for single-fetch
  ingestion (generated pages from the in-memory render, hand-written pages
  from disk — so any docs edit needs a ``make docs`` to refresh it).

Input whitelist (the security rule): the import-cheap spec registry, the
``click.Command`` objects ``build_command`` returns, the manifest modules'
module-level constants, and this package's own static tables. The generator
never reads ``config.yaml`` (or any ``config.*.yaml``) and never calls
``load_config`` — it must produce identical output on a machine with no
config file at all, which is exactly how CI runs it.
"""
