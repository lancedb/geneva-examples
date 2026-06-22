"""Resolve remote-runtime package pins from the installed environment.

The Geneva remote runtime installs a pinned set of pip packages into each
worker's env. Rather than hardcoding versions in every UDF module — where they
silently drift from what's actually installed locally — we read the installed
version of each package and emit an exact ``name==X.Y.Z`` spec. This keeps the
remote workers on the same versions the client resolved (via ``uv.lock``).

An environment variable override (``{PACKAGE}_PACKAGE_SPEC``, e.g.
``GENEVA_PACKAGE_SPEC``) still wins verbatim, so a different build can be
targeted on the workers without touching code.
"""

from __future__ import annotations

import os
from importlib.metadata import version


def package_spec(package: str, *, env_var: str | None = None) -> str:
    """Return ``package==<installed version>``, or an env override if set.

    ``env_var`` defaults to ``{PACKAGE}_PACKAGE_SPEC`` (uppercased package name).
    The override is returned exactly as given — it need not be an ``==`` pin.
    """
    env_var = env_var or f"{package.upper()}_PACKAGE_SPEC"
    override = os.environ.get(env_var)
    if override:
        return override
    return f"{package}=={version(package)}"
