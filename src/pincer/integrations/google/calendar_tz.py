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
instant* (DST-correct); inputs that already carry an offset or ``Z`` keep their
instant and are re-serialised as RFC 3339 (``+0200``, ``+02``, ``Z`` and minute
precision all become ``…:SS+HH:MM``). A wall-clock time that does not exist
(the spring-forward gap, e.g. 03:30 in Kyiv on 2026-03-29) takes the offset in
force before the gap, so it lands one hour later in local time (04:30).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import weakref
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from pincer.core.clock import load_zone, resolve_timezone
from pincer.integrations.google.log_once import LogOnce

if TYPE_CHECKING:
    from collections.abc import Callable
    from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

#: How long a fetched calendar zone is trusted before it is fetched again.
ZONE_CACHE_TTL_S = 300.0
#: How long a failed calendar-zone lookup is not retried.
ZONE_FAILURE_TTL_S = 60.0

SOURCE_CALENDAR = "calendar"
SOURCE_IDENTITY = "identity"
SOURCE_SETTINGS = "settings"

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# One resolver (and so one zone cache) per Google service factory.
_RESOLVERS: weakref.WeakKeyDictionary[Any, CalendarTimezoneResolver] = weakref.WeakKeyDictionary()


def clock_zone(user_timezone: str = "") -> ZoneInfo:
    """The agent clock zone — identity zone, then ``PINCER_TIMEZONE``, then the
    clock's default: the same source as the system-prompt clock."""
    try:
        from pincer.config import get_settings_relaxed

        settings: Any = get_settings_relaxed()
    except Exception as exc:
        logger.warning("Could not load settings for the timezone (%s); using the default zone", exc)
        settings = None
    return resolve_timezone(settings, user_timezone)


def _settings_zone() -> ZoneInfo:
    """``PINCER_TIMEZONE`` (then the clock's default) — the resolver's last resort."""
    return clock_zone("")


def parse_iso(value: str) -> datetime | None:
    """``datetime.fromisoformat`` that returns None instead of raising."""
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def is_date_only(value: str) -> bool:
    """True for a bare ``YYYY-MM-DD`` (a whole day, no time)."""
    return bool(value) and bool(_DATE_ONLY_RE.match(value.strip()))


def needs_zone(value: str) -> bool:
    """True when ``value`` is a date/time without an offset (so a zone is needed)."""
    parsed = parse_iso(value) if value else None
    return parsed is not None and parsed.tzinfo is None


def rfc3339(value: str) -> str:
    """An offset-bearing value re-serialised as RFC 3339; anything else unchanged."""
    parsed = parse_iso(value) if value else None
    if parsed is None or parsed.tzinfo is None:
        return value
    return parsed.isoformat()


def localize(value: str, zone: ZoneInfo) -> str:
    """An event time as RFC 3339: offset-less → ``zone``'s offset at that instant.

    Offset-bearing values keep their instant (re-serialised as RFC 3339);
    date-only (all-day) and unparseable values pass unchanged.
    """
    if not value or _DATE_ONLY_RE.match(value.strip()):
        return value
    parsed = parse_iso(value)
    if parsed is None:
        return value
    if parsed.tzinfo is not None:
        return parsed.isoformat()
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


def local_day_span(time_min: str, time_max: str, zone: ZoneInfo) -> tuple[str, str]:
    """First and last local day a range covers (inclusive): an end bound at
    local midnight belongs to the day before."""

    def _local(bound: str) -> datetime | None:
        parsed = parse_iso(bound) if bound else None
        if parsed is None:
            return None
        return parsed.astimezone(zone) if parsed.tzinfo else parsed

    lo, hi = _local(time_min), _local(time_max)
    if lo is None or hi is None:
        return time_min[:10], time_max[:10]
    first, last = lo.date(), hi.date()
    if hi.time() == datetime.min.time() and last > first:
        last -= timedelta(days=1)
    return first.isoformat(), last.isoformat()


class CalendarTimezoneResolver:
    """Resolve the zone for a calendar and user, caching calendar zones."""

    def __init__(self, ttl_s: float = ZONE_CACHE_TTL_S, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl_s = ttl_s
        self._clock = clock
        # calendar id → (zone name, fetched-at)
        self._cache: dict[str, tuple[str, float]] = {}
        # calendar id → when its last lookup failed
        self._failed_at: dict[str, float] = {}
        # calendar id → its lookup-failure log: one WARNING per outage, then DEBUG
        self._failure_logs: dict[str, LogOnce] = {}

    async def resolve(self, svc: Any, calendar_id: str, user_timezone: str = "") -> tuple[ZoneInfo, str]:
        """``(zone, source)`` — source is ``calendar`` | ``identity`` | ``settings``."""
        zone = await self._calendar_zone(svc, calendar_id or "primary")
        if zone is not None:
            return zone, SOURCE_CALENDAR
        zone = load_zone(user_timezone) if user_timezone else None
        if zone is not None:
            return zone, SOURCE_IDENTITY
        return _settings_zone(), SOURCE_SETTINGS

    async def _calendar_zone(self, svc: Any, calendar_id: str) -> ZoneInfo | None:
        now = self._clock()
        cached = self._cache.get(calendar_id)
        stale = load_zone(cached[0]) if cached is not None else None
        if cached is not None and now - cached[1] < self._ttl_s:
            return stale
        failed_at = self._failed_at.get(calendar_id)
        if failed_at is not None and now - failed_at < ZONE_FAILURE_TTL_S:
            return stale
        try:
            # Best-effort and single-shot: no quota back-off (and its error
            # logs) for a lookup whose failure only means "use the fallback".
            calendar = await asyncio.to_thread(lambda: svc.calendars().get(calendarId=calendar_id).execute())
        except Exception as exc:
            self._failed_at[calendar_id] = now
            self._failure_logs.setdefault(calendar_id, LogOnce(logger)).log(
                "Could not read the timezone of calendar %r (%s); using %s",
                calendar_id,
                exc,
                f"the last known zone {stale.key}" if stale else "the fallback zone",
            )
            return stale
        # Google answered (with or without a zone): any outage is over, the next failure is a new one.
        failure_log = self._failure_logs.get(calendar_id)
        if failure_log is not None:
            failure_log.clear()
        name = calendar.get("timeZone") if isinstance(calendar, dict) else None
        zone = load_zone(name) if isinstance(name, str) else None
        if zone is None:
            self._failed_at[calendar_id] = now
            return stale
        self._failed_at.pop(calendar_id, None)
        self._cache[calendar_id] = (zone.key, now)
        return zone


def resolver_for(factory: Any) -> CalendarTimezoneResolver:
    """The resolver (and zone cache) shared by every tool using ``factory``."""
    resolver = _RESOLVERS.get(factory)
    if resolver is None:
        resolver = CalendarTimezoneResolver()
        _RESOLVERS[factory] = resolver
    return resolver
