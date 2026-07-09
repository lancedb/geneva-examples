"""Configuration loaded from a YAML file.

The YAML file is the single source of truth for the connection *mode*, secrets,
and the connection target shared across the ingest and stage CLIs. See
``config-example.yaml`` (plus ``config-example-local.yaml`` and
``config-example-enterprise.yaml`` for the two mode-specific templates).

Geneva powers **both** modes:

* ``local`` — ``geneva.connect`` against a local on-disk Lance database, running
  backfills on a local Ray instance. No cloud account, no remote cluster, no
  required secrets. A missing ``config.yaml`` resolves to this mode.
* ``enterprise`` — the original path: ``geneva.connect`` against LanceDB Cloud
  (``db://…``) + a remote Geneva runtime. Requires ``lancedb_api_key``,
  ``lancedb_region``, and ``geneva_host``.

Table names are *not* config: each CLI declares its own ``--table-name`` default
(``images`` for the image workflow, ``videos``/``video_clips`` for video,
``pdfs`` for PDFs), so the target table is explicit per command.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_MODE = "local"
DEFAULT_DB_URI = "db://quickstart"
DEFAULT_LOCAL_DB_PATH = "./local_db"

VALID_MODES = ("local", "enterprise")

_TRUTHY = {"true", "1", "yes", "on"}


def _as_bool(value: object) -> bool:
    """Coerce a YAML scalar (native bool or string like ``"false"``) to bool.

    YAML parses bare ``false`` to ``False`` but quoted ``"false"`` to the string
    ``"false"`` (which is truthy), so both forms are normalized here. Absent/None
    defaults to ``False``.
    """
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY
    return bool(value)


@dataclass
class Config:
    """Resolved configuration for the connection mode and S3-compatible storage."""

    mode: str = DEFAULT_MODE
    lancedb_api_key: str | None = None
    lancedb_region: str | None = None
    geneva_host: str | None = None
    db_uri: str = DEFAULT_DB_URI
    local_db_path: str = DEFAULT_LOCAL_DB_PATH
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_endpoint: str | None = None
    s3_region: str | None = None
    aws_allow_http: bool = False
    hf_token: str | None = None

    @property
    def is_local(self) -> bool:
        """True when running against the local Geneva backend (no cloud/cluster)."""
        return self.mode == "local"

    def storage_options(self) -> dict[str, str] | None:
        """Build S3 ``storage_options``; ``None`` unless all four creds present."""
        if not (
            self.s3_access_key
            and self.s3_secret_key
            and self.s3_endpoint
            and self.s3_region
        ):
            return None
        return {
            "aws_access_key_id": self.s3_access_key,
            "aws_secret_access_key": self.s3_secret_key,
            "aws_endpoint": self.s3_endpoint,
            "aws_region": self.s3_region,
            "aws_s3_force_path_style": "true",
            "aws_allow_http": "true" if self.aws_allow_http else "false",
        }


def resolve_mode(mode_override: str | None, data: dict) -> str:
    """Resolve the connection mode by precedence.

    1. ``mode_override`` (e.g. a ``--mode`` CLI flag), if given.
    2. else the config file's ``mode`` key, if given.
    3. else ``enterprise`` when ``geneva_host`` is set (backward compatible),
       otherwise ``local``.
    """
    candidate = mode_override or data.get("mode")
    if candidate:
        mode = str(candidate).lower()
        if mode not in VALID_MODES:
            raise RuntimeError(
                f"invalid mode {candidate!r}; expected one of {VALID_MODES}"
            )
        return mode
    return "enterprise" if data.get("geneva_host") else "local"


def load_config(
    config_path: Path | None = None,
    *,
    mode_override: str | None = None,
) -> Config:
    """Load configuration from ``config_path`` (default: ./config.yaml).

    The mode is resolved via :func:`resolve_mode`. In ``local`` mode the file is
    optional and no secrets are required. In ``enterprise`` mode the file must
    exist and provide ``lancedb_api_key``, ``lancedb_region``, and
    ``geneva_host``.
    """
    if config_path is None:
        config_path = Path("config.yaml")

    data: dict = {}
    if config_path.exists():
        data = yaml.safe_load(config_path.read_text()) or {}

    mode = resolve_mode(mode_override, data)

    if mode == "enterprise":
        if not config_path.exists():
            raise RuntimeError(
                f"config file not found: {config_path} "
                "(enterprise mode requires it — copy config-example-enterprise.yaml "
                "to config.yaml and fill it in, or run in local mode)"
            )
        required = ("lancedb_api_key", "lancedb_region", "geneva_host")
        missing = [key for key in required if not data.get(key)]
        if missing:
            raise RuntimeError(
                f"missing required config in {config_path} for enterprise mode: "
                f"{', '.join(missing)}"
            )

    return Config(
        mode=mode,
        lancedb_api_key=data.get("lancedb_api_key"),
        lancedb_region=data.get("lancedb_region"),
        geneva_host=data.get("geneva_host"),
        db_uri=data.get("db_uri") or DEFAULT_DB_URI,
        local_db_path=data.get("local_db_path") or DEFAULT_LOCAL_DB_PATH,
        s3_access_key=data.get("s3_access_key"),
        s3_secret_key=data.get("s3_secret_key"),
        s3_endpoint=data.get("s3_endpoint"),
        s3_region=data.get("s3_region"),
        aws_allow_http=_as_bool(data.get("aws_allow_http")),
        hf_token=data.get("hf_token") or None,
    )
