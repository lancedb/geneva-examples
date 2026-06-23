"""Shared CLI helpers: logging setup, the Geneva connection, memory sizing."""

from __future__ import annotations

import logging

from geneva_examples.core.config import Config

logger = logging.getLogger(__name__)

# Geneva serializes a UDF/chunker's Ray `memory` request into a signed 32-bit
# field on the namespace API; values >= 2**31 raise OverflowError. `memory` is an
# advisory Ray scheduling reservation, so capping it is safe.
_MEMORY_MAX_BYTES = 2**31 - 1

# Pass as `storage_options` to `create_table` for any table that will source a
# chunker materialized view. Stable row IDs survive compaction, so geneva can map
# clip rows back to source rows on a cross-version refresh; without them, refresh
# only works against the exact source version the view was created from and a
# later refresh fails with "chunker materialized views require stable row IDs for
# cross-version refresh". Create-time only — an existing table can't be retrofitted.
STABLE_ROW_IDS_STORAGE_OPTIONS = {"new_table_enable_stable_row_ids": "true"}


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging once for a CLI invocation."""
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def memory_request_bytes(gib: float) -> int:
    """Bytes for a Ray `memory` request, capped to geneva's 32-bit field limit."""
    requested = int(gib * 1024**3)
    if requested > _MEMORY_MAX_BYTES:
        logger.warning(
            "memory request %.1f GiB exceeds geneva's 32-bit field; capping to %d bytes (~%.2f GiB)",
            gib,
            _MEMORY_MAX_BYTES,
            _MEMORY_MAX_BYTES / 1024**3,
        )
        return _MEMORY_MAX_BYTES
    return requested


def connect(config: Config):
    """Open a Geneva connection from the resolved ``config``."""
    import geneva

    return geneva.connect(
        uri=config.db_uri,
        host_override=config.geneva_host,
        api_key=config.lancedb_api_key,
        region=config.lancedb_region,
        storage_options=config.storage_options(),
    )
