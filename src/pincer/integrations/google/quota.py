"""
Quota / rate-limit handling for Google Workspace API calls.

Google returns HTTP 429 (Too Many Requests) or HTTP 403 with
``reason=rateLimitExceeded`` when quota is hit.  This module provides
an exponential-backoff retry wrapper.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Error codes that indicate a retriable quota/rate-limit error.
_RETRIABLE_STATUS_CODES = {429, 503}
_RETRIABLE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "backendError"}


def _is_retriable(exc: Exception) -> bool:
    """Return True if *exc* looks like a retriable Google API error."""
    try:
        from googleapiclient.errors import HttpError

        if isinstance(exc, HttpError):
            if exc.status_code in _RETRIABLE_STATUS_CODES:
                return True
            # Parse the error details for rate-limit reasons
            details = exc.error_details or []
            for detail in details:
                if isinstance(detail, dict) and detail.get("reason") in _RETRIABLE_REASONS:
                    return True
    except ImportError:
        pass
    return False


async def with_backoff(
    fn: Callable[[], Any],
    max_retries: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 32.0,
) -> Any:
    """Call *fn* (a sync callable) in a thread with exponential backoff.

    Retries on retriable Google API errors (quota / rate-limit).
    Non-retriable errors are raised immediately.

    Args:
        fn: Synchronous callable to execute (e.g. ``lambda: svc.users()...execute()``).
        max_retries: Maximum number of retry attempts.
        base_delay: Initial back-off delay in seconds.
        max_delay: Cap on back-off delay.

    Returns:
        Whatever *fn* returns.
    """
    delay = base_delay
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            return await asyncio.to_thread(fn)
        except Exception as exc:
            if not _is_retriable(exc):
                raise
            last_exc = exc
            if attempt < max_retries:
                jitter = random.uniform(0, delay * 0.1)
                wait = min(delay + jitter, max_delay)
                logger.warning(
                    "Google API quota error (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1, max_retries, wait, exc,
                )
                await asyncio.sleep(wait)
                delay = min(delay * 2, max_delay)
            else:
                logger.error("Google API quota error after %d retries: %s", max_retries, exc)

    raise last_exc  # type: ignore[misc]
