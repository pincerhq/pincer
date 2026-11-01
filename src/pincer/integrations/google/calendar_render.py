"""
Event renderer — what Pincer tells the user about their calendar.

Every time shown is what Google stored, converted to one zone (the resolved
calendar zone) that is named once, so the model never has to guess which
clock a time belongs to. All-day events say "all day"; every event keeps an
``ID:`` line so a later change can address it. When the calendar's zone is not
the agent's clock zone (the zone the system-prompt clock uses), one note line
says so — otherwise "10:00" in a Kyiv calendar reads as 10:00 Berlin.

``render_agenda`` and ``render_reminder`` are for people, not the model (the
morning briefing and the upcoming-event reminder): 24-hour local times, the
zone named once, no ISO stamps and no ``ID:`` lines.

Format contract (parsed by the voice flow, keep verbatim):
- event lines: ``  <start ISO> → <end ISO> — <title>[ | …]`` (voice tool speech);
- write confirmations: a ``Link: <url>`` line (voice booking link extraction).
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pincer.core.clock import load_zone

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

ALL_DAY = "all day"
NO_TITLE = "(no title)"
# Literals the voice flow parses (tool speech's event-line regex, the booking
# flow's ``Link: (\S+)``) — change them only together with those parsers.
RANGE_ARROW = " → "
TITLE_DASH = " — "
LINK_PREFIX = "Link: "
#: Shown when a write response carries an id but no start/end.
STORED_TIMES_UNAVAILABLE = "(stored times unavailable)"

# What the zone a result is rendered in actually is — the mismatch note names it.
CALENDAR_ZONE_LABEL = "this calendar's timezone"
EVENT_ZONE_LABEL = "the event's timezone"
CONFIGURED_ZONE_LABEL = "the configured timezone"

# Last line of every read result that shows events: without an ID in hand the
# model "changed" events by creating a second one ("Meeting (updated)").
EDIT_HINT = (
    "To change one of these events (rename, reschedule, new time or details), call google__update_event "
    "with its ID; never create a new event for a change."
)

# Human-facing renderers (briefing, reminder).
CLOCK_FORMAT = "%H:%M"
AGENDA_DAY_FORMAT = "%A %d %B"
AGENDA_RANGE = "–"
NO_EVENTS_TODAY = "No events today."


def strip_edit_hint(text: str) -> str:
    """A read result without its trailing ``EDIT_HINT`` — for MCP clients, which lack the update tool."""
    return text.removesuffix(f"\n{EDIT_HINT}")


def _instant(field: dict[str, Any]) -> datetime | None:
    raw = field.get("dateTime")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _all_day(field: dict[str, Any]) -> date | None:
    raw = field.get("date")
    if not raw or field.get("dateTime"):
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return None


def _local(field: dict[str, Any], zone: ZoneInfo) -> str:
    """One stored time in ``zone`` (ISO with offset), or the raw value if unparseable."""
    instant = _instant(field)
    if instant is not None:
        return instant.astimezone(zone).isoformat()
    return str(field.get("dateTime") or field.get("date") or "")


def _all_day_span(event: dict[str, Any]) -> tuple[date, date] | None:
    """First and last day (inclusive) of an all-day event; Google's end date is exclusive."""
    first = _all_day(event.get("start") or {})
    if first is None:
        return None
    end = _all_day(event.get("end") or {})
    last = end - timedelta(days=1) if end is not None and end > first else first
    return first, last


def start_instant(event: dict[str, Any]) -> datetime | None:
    """A timed event's start as an aware instant (None for all-day or unparseable)."""
    return _instant(event.get("start") or {})


def event_instants(events: list[dict[str, Any]]) -> list[datetime]:
    """The timed start/end instants of ``events`` (for zone-equivalence checks)."""
    found: list[datetime] = []
    for event in events:
        for key in ("start", "end"):
            instant = _instant(event.get(key) or {})
            if instant is not None:
                found.append(instant)
    return found


def zones_equivalent(a: ZoneInfo, b: ZoneInfo, instants: list[datetime]) -> bool:
    """Same zone by key, or the same UTC offset at every instant that matters.

    Google still reports some calendars under legacy aliases (``Europe/Kiev``)
    while the agent clock uses the current name (``Europe/Kyiv``): those are
    one zone and must not trigger the mismatch note.
    """
    if a.key == b.key:
        return True
    moments = instants or [datetime.now(UTC)]
    return all(m.astimezone(a).utcoffset() == m.astimezone(b).utcoffset() for m in moments)


def mismatch_note(
    zone: ZoneInfo,
    clock_zone: ZoneInfo | None,
    instants: list[datetime],
    zone_label: str = CALENDAR_ZONE_LABEL,
) -> str:
    """One note line when the render zone is not the agent clock zone, else "".

    ``zone_label`` says what the render zone is (the calendar's, a caller-given
    event zone, or the configured fallback) so the note never misnames it.
    """
    if clock_zone is None or not instants or zones_equivalent(zone, clock_zone, instants):
        # No timed value rendered (e.g. only all-day events): nothing to warn about.
        return ""
    return f"Note: times are in {zone_label} ({zone.key}), which differs from your clock timezone ({clock_zone.key})."


def render_event_line(event: dict[str, Any], zone: ZoneInfo) -> str:
    """One list entry: the time line plus its ``ID:`` line."""
    start, end, all_day = _when(event, zone)
    when = f"{start or STORED_TIMES_UNAVAILABLE}{RANGE_ARROW}{end or STORED_TIMES_UNAVAILABLE}"
    extras = [ALL_DAY] if all_day else []
    if event.get("location"):
        extras.append(str(event["location"]))
    attendees = len(event.get("attendees") or [])
    if attendees:
        extras.append(f"{attendees} attendee(s)")
    suffix = "".join(f" | {x}" for x in extras)
    return f"  {when}{TITLE_DASH}{_title(event)}{suffix}\n  ID: {event.get('id', '')}"


def render_events(
    events: list[dict[str, Any]],
    zone: ZoneInfo,
    *,
    header: str,
    clock_zone: ZoneInfo | None = None,
    zone_label: str = CALENDAR_ZONE_LABEL,
    more: bool = False,
) -> str:
    """A list/search result: header naming the zone, optional note, one entry per event."""
    parts = [f"{header}, times in {zone.key}:"]
    note = mismatch_note(zone, clock_zone, event_instants(events), zone_label)
    if note:
        parts.append(note)
    parts.extend(render_event_line(e, zone) for e in events)
    if more:
        parts.append("(more results available — use pagination parameters)")
    return "\n".join(parts)


def _title(event: dict[str, Any]) -> str:
    return str(event.get("summary") or NO_TITLE)


def _when(event: dict[str, Any], zone: ZoneInfo) -> tuple[str, str, bool]:
    """Start, end ("" when absent) and whether the event is all-day (inclusive last day)."""
    span = _all_day_span(event)
    if span is not None:
        return span[0].isoformat(), span[1].isoformat(), True
    return _local(event.get("start") or {}, zone), _local(event.get("end") or {}, zone), False


def _when_labelled(event: dict[str, Any], zone: ZoneInfo) -> tuple[str, str]:
    """Start/end for details and confirmations: all-day values say so."""
    start, end, all_day = _when(event, zone)
    if all_day:
        return f"{start} ({ALL_DAY})", f"{end} ({ALL_DAY})"
    return start, end


def stored_zone_name(event: dict[str, Any], key: str = "start") -> str:
    """The event's own stored ``timeZone`` for its start/end ("" if it has none)."""
    return str((event.get(key) or {}).get("timeZone") or "")


def _same_zone_name(name: str, zone: ZoneInfo, instants: list[datetime]) -> bool:
    own = load_zone(name)
    return own is not None and zones_equivalent(own, zone, instants)


def render_event_details(
    event: dict[str, Any],
    zone: ZoneInfo,
    *,
    clock_zone: ZoneInfo | None = None,
    zone_label: str = CALENDAR_ZONE_LABEL,
) -> str:
    """A single event (get)."""
    start, end = _when_labelled(event, zone)
    lines = [
        f"Title:       {_title(event)}",
        f"Start:       {start or STORED_TIMES_UNAVAILABLE}",
        f"End:         {end or STORED_TIMES_UNAVAILABLE}",
        f"Times shown in: {zone.key}",
    ]
    own = stored_zone_name(event)
    if own and not _same_zone_name(own, zone, event_instants([event])):
        # Kept on purpose (an update keeps it too). This — not the display
        # zone above — is the zone to pass when changing the event's times.
        lines.append(f"Event's own timezone: {own}")
    note = mismatch_note(zone, clock_zone, event_instants([event]), zone_label)
    if note:
        lines.append(note)
    lines += [
        f"Location:    {event.get('location', '')}",
        f"Description: {event.get('description', '')}",
        f"Status:      {event.get('status', '')}",
        f"Organizer:   {event.get('organizer', {}).get('email', '')}",
        f"ID:          {event.get('id', '')}",
        f"Link:        {event.get('htmlLink', '')}",
    ]
    attendees = event.get("attendees", [])
    if attendees:
        lines.append(f"Attendees ({len(attendees)}):")
        for att in attendees:
            lines.append(f"  {att.get('email', '')} [{att.get('responseStatus', '?')}]")
    return "\n".join(lines)


def render_write(
    headline: str,
    event: dict[str, Any],
    zone: ZoneInfo,
    *,
    clock_zone: ZoneInfo | None = None,
    zone_label: str = CALENDAR_ZONE_LABEL,
    extra: str = "",
) -> str:
    """A write confirmation from Google's stored copy: stored start/end + zone, ID, link."""
    start, end = _when_labelled(event, zone)
    if not start and not end:
        lines = [headline, f"Start: {STORED_TIMES_UNAVAILABLE}"]
    else:
        shown_start, shown_end = start or STORED_TIMES_UNAVAILABLE, end or STORED_TIMES_UNAVAILABLE
        lines = [headline, f"Start: {shown_start} | End: {shown_end} ({zone.key})"]
    note = mismatch_note(zone, clock_zone, event_instants([event]), zone_label)
    if note:
        lines.append(note)
    lines += [f"ID: {event.get('id', '')}", f"{LINK_PREFIX}{event.get('htmlLink', '')}"]
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def _clock_time(field: dict[str, Any], zone: ZoneInfo) -> str:
    instant = _instant(field)
    return instant.astimezone(zone).strftime(CLOCK_FORMAT) if instant is not None else "?"


def _agenda_line(event: dict[str, Any], zone: ZoneInfo) -> str:
    if _all_day_span(event) is not None:
        when = ALL_DAY
    else:
        start = _clock_time(event.get("start") or {}, zone)
        when = f"{start}{AGENDA_RANGE}{_clock_time(event.get('end') or {}, zone)}"
    location = f" | {event['location']}" if event.get("location") else ""
    return f"  {when}  {_title(event)}{location}"


def render_agenda(events: list[dict[str, Any]], zone: ZoneInfo, day: date) -> str:
    """A day's events for a person (the briefing): one header naming the zone, then one line each."""
    lines = [f"Calendar — {day.strftime(AGENDA_DAY_FORMAT)} ({zone.key})"]
    if events:
        lines.extend(_agenda_line(e, zone) for e in events)
    else:
        lines.append(f"  {NO_EVENTS_TODAY}")
    return "\n".join(lines)


def render_reminder(event: dict[str, Any], zone: ZoneInfo, now: datetime) -> str:
    """One upcoming-event reminder for a person, its start in the calendar's zone."""
    start = start_instant(event)
    at = start.astimezone(zone).strftime(CLOCK_FORMAT) if start is not None else "?"
    minutes = math.ceil((start - now).total_seconds() / 60) if start is not None else 0
    when = f"in {minutes} min" if minutes > 0 else "now"
    text = f"Reminder: {_title(event)} starts at {at} ({zone.key}), {when}."
    if event.get("location"):
        text += f" Location: {event['location']}."
    return text
