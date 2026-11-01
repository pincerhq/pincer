"""
Voice-facing time rendering — DST-safe local time for spoken/reported times.

All timestamps are stored in UTC; anything spoken to a caller or reported in a
call summary must be rendered in the configured zone
(PINCER_VOICE_TIMEZONE, falling back to settings.timezone, then Europe/Berlin).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from pincer.integrations.google.calendar_tz import is_date_only, localize, needs_zone

if TYPE_CHECKING:
    from pincer.config import Settings

DEFAULT_VOICE_TIMEZONE = "Europe/Berlin"
# The call prompt (TIME_CONTEXT) tells the model to give calendar times "as local
# wall-clock time in <voice zone>". A new event is booked in the voice zone (its
# ``timezone`` argument) ...
VOICE_ZONED_CALENDAR_CREATES = frozenset({"google__create_event"})
# ... while these keep their own zone choice and only get the voice zone's offset on
# offset-less times: an update keeps the event's stored zone (a New York event moved on
# a call stays a New York event), and an availability check covers the slot a booking
# would take.
VOICE_LOCAL_TIME_ARGS: dict[str, tuple[str, ...]] = {
    "google__update_event": ("start", "end"),
    "google__list_events": ("time_min", "time_max"),
    "google__check_freebusy": ("time_min", "time_max"),
}


def get_voice_timezone(settings: Settings) -> ZoneInfo:
    """Resolve the timezone used for all voice-facing time rendering."""
    for tz_name in (
        getattr(settings, "voice_timezone", "") or "",
        getattr(settings, "timezone", "") or "",
        DEFAULT_VOICE_TIMEZONE,
    ):
        if not tz_name.strip():
            continue
        try:
            return ZoneInfo(tz_name.strip())
        except (KeyError, ValueError):
            continue
    return ZoneInfo(DEFAULT_VOICE_TIMEZONE)


def with_voice_zone(tool_name: str, arguments: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """A call's calendar arguments with offset-less times read in the voice zone, as the call prompt says.

    Without it the calendar tools read offset-less times in the calendar's own
    zone: a caller's "14:00" on a Kyiv calendar of a Berlin deployment would be
    booked (or checked) at 14:00 Kyiv (13:00 Berlin). A create gets ``timezone``
    set to the voice zone; an update and the reads get the voice zone's offset
    on their offset-less date-times (a bare date stays as given: an all-day
    move, or the calendar's day). A zone the model named is never overridden.
    Other tools' arguments are returned as is.
    """
    if str(arguments.get("timezone") or "").strip():
        return arguments
    zone = get_voice_timezone(settings)
    if tool_name in VOICE_ZONED_CALENDAR_CREATES:
        return {**arguments, "timezone": zone.key}
    pinned = {
        key: localize(value, zone)
        for key in VOICE_LOCAL_TIME_ARGS.get(tool_name, ())
        if isinstance(value := arguments.get(key), str) and needs_zone(value) and not is_date_only(value)
    }
    return {**arguments, **pinned} if pinned else arguments


def to_voice_local(dt: datetime, settings: Settings) -> datetime:
    """Convert a datetime to the voice timezone (naive input is taken as UTC)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(get_voice_timezone(settings))


def voice_now(settings: Settings) -> datetime:
    """Current time in the voice timezone."""
    return datetime.now(get_voice_timezone(settings))


def format_voice_time(dt: datetime, settings: Settings, language: str = "en") -> str:
    """Render a datetime for speech, e.g. '14:30 on Monday, March 30'."""
    local = to_voice_local(dt, settings)
    if language.lower().startswith("de"):
        return local.strftime("%H:%M Uhr am %d.%m.%Y")
    return local.strftime("%H:%M on %A, %B %d")


def voice_today_str(settings: Settings) -> str:
    """Today's date (YYYY-MM-DD) in the voice timezone — for daily counters."""
    return voice_now(settings).strftime("%Y-%m-%d")
