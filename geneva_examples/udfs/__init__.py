"""Geneva UDF factories, one module per model.

Each module exposes a ``build_*`` factory that returns a configured
``@geneva.udf``-decorated instance plus its own env-overridable ``*_RUNTIME_PIP``
list. UDFs are built inside the factory and kept fully self-contained (all
imports inside ``setup()``/``__call__``, helpers nested in the closure) because
this package is **not** importable on the remote Geneva runtime — only the
manifest's pip packages are. Import from the submodules directly so importing
this package stays light.
"""
