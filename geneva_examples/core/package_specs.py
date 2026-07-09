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
import re
from importlib.metadata import version


def _default_env_var(package: str) -> str:
    """``{PACKAGE}_PACKAGE_SPEC`` with non-alphanumerics normalized to ``_``.

    Distribution names may contain ``-``/``.`` (e.g. ``open-clip-torch``), which
    are illegal in shell environment-variable names — collapse them to ``_`` so
    the override key is always a valid identifier (``OPEN_CLIP_TORCH_PACKAGE_SPEC``).
    """
    return re.sub(r"[^0-9A-Za-z]+", "_", package).upper() + "_PACKAGE_SPEC"


def package_spec(package: str, *, env_var: str | None = None) -> str:
    """Return ``package==<installed version>``, or an env override if set.

    ``env_var`` defaults to ``{PACKAGE}_PACKAGE_SPEC`` (uppercased, with any
    ``-``/``.`` normalized to ``_``). The override is returned exactly as given —
    it need not be an ``==`` pin.
    """
    env_var = env_var or _default_env_var(package)
    override = os.environ.get(env_var)
    if override:
        return override
    return f"{package}=={version(package)}"
