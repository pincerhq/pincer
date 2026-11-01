"""
Current-date-and-time context for the agent.

A language model has no clock. Left to itself it answers "what's today?" from
the shape of its training data — confidently, and wrong by months. Nothing else
in the prompt contradicts it, so the invented date then propagates into every
relative expression the turn produces: "tomorrow" in a calendar event, "last
week" in a search, the deadline in a drafted email.

So every system prompt states the real time, and states it in the *user's* zone
rather than the server's — the two differ as soon as Pincer is hosted anywhere
but the user's own machine, and "today" is a property of where the user is.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

#: Matches the default of `Settings.timezone`; used only if that is unset too.
DEFAULT_TIMEZONE = "Europe/Berlin"
#: Tool-context key for the zone of a phone call (the voice zone): calendar tools read
#: offset-less times in it and show times in it. Set only on calls; ``timezone`` in the
#: same context stays the user's identity zone.
CALL_TIMEZONE_KEY = "call_timezone"


def load_zone(name: str) -> ZoneInfo | None:
    """``ZoneInfo`` for one IANA name, or None for a blank or unknown one.

    Never raises: an unknown or misspelled name is logged, so callers can fall
    back to their next candidate.
    """
    cleaned = str(name or "").strip()
    if not cleaned:
        return None
    try:
        return ZoneInfo(cleaned)
    except (KeyError, ValueError, ZoneInfoNotFoundError):
        logger.warning("Unknown timezone %r — falling back to the next candidate", cleaned)
        return None


def resolve_timezone(settings: Any, override: str = "") -> ZoneInfo:
    """The zone to render times in: per-user override > settings > default.

    Never raises: an unknown or misspelled zone name is logged and skipped, so
    a bad `timezone` value costs accuracy, not the whole conversation.
    """
    for name in (override, getattr(settings, "timezone", "") or "", DEFAULT_TIMEZONE):
        zone = load_zone(name)
        if zone is not None:
            return zone
    return ZoneInfo(DEFAULT_TIMEZONE)


def current_time_block(settings: Any, override: str = "", now: datetime | None = None) -> str:
    """The system-prompt block carrying the current date and time.

    `now` is injectable so tests don't depend on the wall clock.
    """
    tz = resolve_timezone(settings, override)
    moment = (now or datetime.now(UTC)).astimezone(tz)
    # Explicit weekday and named month: "03/04" is ambiguous across locales and
    # the model will read it the wrong way round often enough to matter.
    stamp = moment.strftime("%A, %d %B %Y, %H:%M")
    return (
        "[Current date and time]\n"
        f"{stamp} ({moment.tzname()}, {tz.key})\n"
        "This is the real current time in the user's timezone. Resolve every relative "
        "expression against it — today, tomorrow, this week, next Friday, how long ago. "
        "Your training data is not a clock: never infer the date from it, and never "
        "state a date or time that this block or a tool result did not give you."
    )
