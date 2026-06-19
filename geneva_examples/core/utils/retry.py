"""Retry helper for flaky table writes."""

import logging
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)


def retry_io(
    label: str,
    operation: Callable,
    attempts: int = 5,
    sleep_s: float = 2.0,
):
    """Run ``operation`` with linear backoff, retrying up to ``attempts`` times."""
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt == attempts:
                break
            wait_s = sleep_s * attempt
            logger.warning(
                "table_write_retry %s attempt %d of %d sleep_s %s error %s",
                label,
                attempt,
                attempts,
                wait_s,
                type(exc).__name__,
            )
            time.sleep(wait_s)
    raise last_exc
