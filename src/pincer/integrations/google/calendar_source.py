"""
The Google factory for background calendar readers (briefing, reminder, MCP alias).

These readers honour the ``[integrations.google]`` switch exactly like the
tools: the factory comes from ``get_google_factory`` (None when the integration
is disabled or no token is configured), and calendar reads also need
``calendar`` among the services the factory serves. Switched off means no
calendar call at all.

The factory is resolved once, lazily, and cached — including "unavailable" —
for the life of the reader, like the tool registry: a token added later needs
a restart. Re-resolving on every 60-second tick would re-probe the token and
log "token loaded" each minute. Why the calendar is unavailable is logged once
at INFO, then at debug.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pincer.integrations.google.log_once import LogOnce

if TYPE_CHECKING:
    from pincer.integrations.google.service_factory import GoogleServiceFactory

CALENDAR_SERVICE = "calendar"

logger = logging.getLogger(__name__)


class CalendarSource:
    """A lazily resolved, cached Google factory that may serve calendar reads."""

    def __init__(self, reader: str) -> None:
        self._reader = reader
        self._resolved = False
        self._factory: GoogleServiceFactory | None = None
        self._reason = ""
        self._unavailable_log = LogOnce(logger, logging.INFO)

    def factory(self) -> GoogleServiceFactory | None:
        """The factory, or None when calendar reads are off (the reason is logged)."""
        if not self._resolved:
            self._resolved = True
            self._factory, self._reason = _resolve()
        if self._factory is None:
            self._unavailable_log.log("Calendar unavailable for %s: %s", self._reader, self._reason)
        return self._factory


def _resolve() -> tuple[GoogleServiceFactory | None, str]:
    from pincer.integrations.google import get_google_factory

    try:
        factory = get_google_factory()
    except Exception as exc:
        return None, f"Google setup failed ({type(exc).__name__}: {exc})"
    if factory is None:
        return None, "the Google integration is disabled or has no token"
    if CALENDAR_SERVICE not in factory.auth.services:
        return None, "calendar is not an enabled Google service"
    return factory, ""
