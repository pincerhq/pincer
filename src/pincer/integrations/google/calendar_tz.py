"""
Calendar timezone resolver — which zone an offset-less calendar time means.

The model writes calendar times the way people say them: "2026-11-12T10:00:00",
no offset. That wall-clock time belongs to the calendar it is written to — a
Kyiv calendar's "10:00" is 10:00 in Kyiv even when the deployment runs on
Berlin time. Google also refuses offset-less ``timeMin``/``timeMax`` outright
(HTTP 400), so list/search/free-busy bounds must always carry an offset.

Resolution order: the target calendar's own ``timeZone`` (fetched from the
Calendar API, cached per calendar id for a few minutes) → the per-user identity
timezone → ``PINCER_TIMEZONE``. A failed calendar fetch falls through to the
next source and never fails the tool call.

Conversion: offset-less inputs get the resolved zone's offset *for that
instant* (DST-correct); inputs that already carry an offset or ``Z`` are
returned unchanged, byte for byte.
"""

from __future__ import annotations

import logging
import re
import time
import weakref
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pincer.integrations.google.quota import with_backoff

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

#: How long a fetched calendar zone is trusted before it is fetched again.
ZONE_CACHE_TTL_S = 300.0

SOURCE_CALENDAR = "calendar"
SOURCE_IDENTITY = "identity"
SOURCE_SETTINGS = "settings"

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# One resolver (and so one zone cache) per Google service factory.
_RESOLVERS: weakref.WeakKeyDictionary[Any, CalendarTimezoneResolver] = weakref.WeakKeyDictionary()


def zone_or_none(name: object) -> ZoneInfo | None:
    """``ZoneInfo(name)`` for a valid IANA name, else None (never raises)."""
    if not isinstance(name, str) or not name.strip():
        return None
    try:
        return ZoneInfo(name.strip())
    except (KeyError, ValueError, ZoneInfoNotFoundError):
        logger.warning("Unknown timezone %r — falling back to the next source", name)
        return None


def _settings_zone() -> ZoneInfo:
    """``PINCER_TIMEZONE`` (then the clock's default) — the last resort."""
    from pincer.core.clock import resolve_timezone

    try:
        from pincer.config import get_settings_relaxed

        settings: Any = get_settings_relaxed()
    except Exception:  # pragma: no cover - settings that cannot load at all
        settings = None
    return resolve_timezone(settings)


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def needs_zone(value: str) -> bool:
    """True when ``value`` is a date/time without an offset (so a zone is needed)."""
    parsed = _parse(value) if value else None
    return parsed is not None and parsed.tzinfo is None


def localize(value: str, zone: ZoneInfo) -> str:
    """An event time as RFC 3339: offset-less → ``zone``'s offset at that instant.

    Offset-bearing, date-only (all-day) and unparseable values pass unchanged.
    """
    if not value or _DATE_ONLY_RE.match(value.strip()):
        return value
    parsed = _parse(value)
    if parsed is None or parsed.tzinfo is not None:
        return value
    return parsed.replace(tzinfo=zone).isoformat()


def localize_bound(value: str, zone: ZoneInfo, *, end: bool = False) -> str:
    """A range bound as RFC 3339 with an offset (Google 400s without one).

    A bare date means that whole local day: midnight for a start bound, the
    next midnight for an end bound (so 12th..12th lists the 12th).
    """
    if not value:
        return value
    if _DATE_ONLY_RE.match(value.strip()):
        day = datetime.fromisoformat(value.strip())
        if end:
            day += timedelta(days=1)
        return day.replace(tzinfo=zone).isoformat()
    return localize(value, zone)


class CalendarTimezoneResolver:
    """Resolve the zone for a calendar and user, caching calendar zones."""

    def __init__(self, ttl_s: float = ZONE_CACHE_TTL_S, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl_s = ttl_s
        self._clock = clock
        # calendar id → (zone name, fetched-at)
        self._cache: dict[str, tuple[str, float]] = {}

    async def resolve(self, svc: Any, calendar_id: str, user_timezone: str = "") -> tuple[ZoneInfo, str]:
        """``(zone, source)`` — source is ``calendar`` | ``identity`` | ``settings``."""
        zone = await self._calendar_zone(svc, calendar_id or "primary")
        if zone is not None:
            return zone, SOURCE_CALENDAR
        zone = zone_or_none(user_timezone) if user_timezone else None
        if zone is not None:
            return zone, SOURCE_IDENTITY
        return _settings_zone(), SOURCE_SETTINGS

    async def _calendar_zone(self, svc: Any, calendar_id: str) -> ZoneInfo | None:
        cached = self._cache.get(calendar_id)
        if cached is not None and self._clock() - cached[1] < self._ttl_s:
            return zone_or_none(cached[0])
        try:
            # One attempt only: this lookup is best-effort and must not stall
            # the tool behind quota back-off.
            calendar = await with_backoff(
                lambda: svc.calendars().get(calendarId=calendar_id).execute(),
                max_retries=0,
            )
        except Exception as exc:
            logger.warning("Could not read the timezone of calendar %r: %s", calendar_id, exc)
            return None
        name = calendar.get("timeZone") if isinstance(calendar, dict) else None
        zone = zone_or_none(name)
        if zone is not None:
            self._cache[calendar_id] = (zone.key, self._clock())
        return zone


def resolver_for(factory: Any) -> CalendarTimezoneResolver:
    """The resolver (and zone cache) shared by every tool using ``factory``."""
    resolver = _RESOLVERS.get(factory)
    if resolver is None:
        resolver = CalendarTimezoneResolver()
        _RESOLVERS[factory] = resolver
    return resolver
