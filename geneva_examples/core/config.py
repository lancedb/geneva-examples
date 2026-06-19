"""Configuration loaded from a YAML file.

The YAML file is the single source of truth for secrets, the connection target,
and the table identity shared across the ingest and stage CLIs. See
``config-example.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_DB_URI = "db://quickstart"
DEFAULT_TABLE_NAME = "images"


@dataclass
class Config:
    """Resolved configuration for LanceDB Cloud, S3-compatible storage, and the table."""

    lancedb_api_key: str
    lancedb_region: str
    geneva_host: str
    db_uri: str
    table_name: str
    s3_access_key: str | None
    s3_secret_key: str | None
    s3_endpoint: str | None
    s3_region: str | None
    aws_allow_http: str
    hf_token: str | None

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
            "aws_allow_http": self.aws_allow_http,
        }


def load_config(config_path: Path | None = None) -> Config:
    """Load configuration from ``config_path`` (default: ./config.yaml).

    Raises if the file is missing or any required field is absent
    (``lancedb_api_key``, ``lancedb_region``, ``geneva_host``).
    """
    if config_path is None:
        config_path = Path("config.yaml")

    if not config_path.exists():
        raise RuntimeError(
            f"config file not found: {config_path} "
            "(copy config-example.yaml to config.yaml and fill it in)"
        )

    data = yaml.safe_load(config_path.read_text()) or {}

    required = ("lancedb_api_key", "lancedb_region", "geneva_host")
    missing = [key for key in required if not data.get(key)]
    if missing:
        raise RuntimeError(
            f"missing required config in {config_path}: {', '.join(missing)}"
        )

    return Config(
        lancedb_api_key=data["lancedb_api_key"],
        lancedb_region=data["lancedb_region"],
        geneva_host=data["geneva_host"],
        db_uri=data.get("db_uri") or DEFAULT_DB_URI,
        table_name=data.get("table_name") or DEFAULT_TABLE_NAME,
        s3_access_key=data.get("s3_access_key"),
        s3_secret_key=data.get("s3_secret_key"),
        s3_endpoint=data.get("s3_endpoint"),
        s3_region=data.get("s3_region"),
        aws_allow_http=data.get("aws_allow_http") or "false",
        hf_token=data.get("hf_token") or None,
    )
