"""
Quota / rate-limit handling for Google Workspace API calls.

Google returns HTTP 429 (Too Many Requests) or HTTP 403 with
``reason=rateLimitExceeded`` when quota is hit.  This module provides
an exponential-backoff retry wrapper.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

T = TypeVar("T")

# The one source for reading a Google HttpError (the retry wrapper here, the reminder loop's
# outage check in scheduler/triggers). Retried on the spot: short-lived limits and hiccups.
_RETRIABLE_STATUS_CODES = frozenset({429, 503})
RETRIABLE_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "backendError"})
# Exhausted quotas last until Google resets them (daily): never retried, but an outage, not a bug.
EXHAUSTED_QUOTA_REASONS = frozenset({"quotaExceeded", "dailyLimitExceeded"})
_SERVER_ERROR_STATUS = 500
_UPPER_SNAKE_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*$")


def _http_error(exc: BaseException) -> Any:
    """``exc`` when it is a Google ``HttpError``, else None."""
    try:
        from googleapiclient.errors import HttpError
    except ImportError:
        return None
    return exc if isinstance(exc, HttpError) else None


def _reason_name(raw: str) -> str:
    """A reason as the legacy ``errors[].reason`` spells it: google.rpc ErrorInfo's UPPER_SNAKE
    (``RATE_LIMIT_EXCEEDED``) becomes camelCase (``rateLimitExceeded``); others pass unchanged."""
    if not _UPPER_SNAKE_RE.match(raw):
        return raw
    first, *rest = raw.lower().split("_")
    return first + "".join(part.capitalize() for part in rest)


def _error_entries(error: Any) -> list[Any]:
    """Both reason sources of an error body: the legacy ``error.errors[]`` and google.rpc
    ``error.details[]`` (ErrorInfo). googleapiclient's ``error_details`` holds only the first of
    ``detail``/``details``/``errors``/``message`` present, so ``details`` hides ``errors``."""
    entries = list(error.error_details) if isinstance(error.error_details, list) else []
    try:
        data = json.loads(error.content.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, ValueError):
        return entries
    body = data[0] if isinstance(data, list) and data else data
    inner = body.get("error") if isinstance(body, dict) else None
    if isinstance(inner, dict):
        for key in ("errors", "details"):
            if isinstance(inner.get(key), list):
                entries.extend(inner[key])
    return entries


def error_reasons(exc: BaseException) -> set[str]:
    """The ``reason`` fields of a Google ``HttpError``, from ``errors[]`` and ErrorInfo ``details[]``
    alike, in camelCase (empty for anything else)."""
    error = _http_error(exc)
    if error is None:
        return set()
    return {
        _reason_name(str(entry["reason"]))
        for entry in _error_entries(error)
        if isinstance(entry, dict) and entry.get("reason")
    }


def _is_retriable(exc: Exception) -> bool:
    """Return True if *exc* looks like a retriable Google API error."""
    error = _http_error(exc)
    if error is None:
        return False
    return error.status_code in _RETRIABLE_STATUS_CODES or bool(error_reasons(exc) & RETRIABLE_REASONS)


def is_google_outage(exc: BaseException) -> bool:
    """True when a Google ``HttpError`` means Google is down or limiting us (5xx, 429, a rate
    limit or an exhausted quota) rather than a bad request."""
    error = _http_error(exc)
    if error is None:
        return False
    if error.status_code >= _SERVER_ERROR_STATUS or error.status_code in _RETRIABLE_STATUS_CODES:
        return True
    return bool(error_reasons(exc) & (RETRIABLE_REASONS | EXHAUSTED_QUOTA_REASONS))


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
                    attempt + 1,
                    max_retries,
                    wait,
                    exc,
                )
                await asyncio.sleep(wait)
                delay = min(delay * 2, max_delay)
            else:
                logger.error("Google API quota error after %d retries: %s", max_retries, exc)

    raise last_exc  # type: ignore[misc]
