"""Retry helper for flaky table writes."""

import logging
import random
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)


def retry_io(
    label: str,
    operation: Callable,
    attempts: int = 5,
    sleep_s: float = 2.0,
    retry_on: type[Exception] | tuple[type[Exception], ...] = Exception,
    jitter: float = 0.1,
):
    """Run ``operation`` with linear backoff, retrying up to ``attempts`` times.

    Only exceptions matching ``retry_on`` are retried; anything else (e.g. a bad
    API key or a programming error) propagates immediately instead of burning the
    full backoff. ``jitter`` adds up to ``jitter * wait`` random seconds to each
    sleep so parallel callers don't retry in lockstep (thundering herd).
    """
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except retry_on as exc:
            if attempt == attempts:
                raise
            wait_s = sleep_s * attempt
            if jitter:
                # Non-crypto jitter to desynchronize parallel retries.
                wait_s += random.uniform(0, jitter * wait_s)  # noqa: S311
            logger.warning(
                "table_write_retry %s attempt %d of %d sleep_s %.2f error %s",
                label,
                attempt,
                attempts,
                wait_s,
                type(exc).__name__,
            )
            time.sleep(wait_s)
