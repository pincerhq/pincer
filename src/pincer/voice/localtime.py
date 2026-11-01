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

if TYPE_CHECKING:
    from pincer.config import Settings

DEFAULT_VOICE_TIMEZONE = "Europe/Berlin"
# Calendar writes whose offset-less times the call prompt (TIME_CONTEXT) tells the
# model to give "as local wall-clock time in <voice zone>".
VOICE_ZONED_CALENDAR_WRITES = frozenset({"google__create_event", "google__update_event"})


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
    """A call's calendar-write arguments with ``timezone`` set to the voice zone unless the model gave one.

    Without it the calendar tools read offset-less times in the calendar's own
    zone: a caller's "14:00" on a Kyiv calendar of a Berlin deployment would be
    booked at 14:00 Kyiv (13:00 Berlin). Other tools' arguments are returned as is.
    """
    if tool_name not in VOICE_ZONED_CALENDAR_WRITES or str(arguments.get("timezone") or "").strip():
        return arguments
    return {**arguments, "timezone": get_voice_timezone(settings).key}


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
