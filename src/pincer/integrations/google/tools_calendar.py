"""
Google Calendar tools — 12 tools for reading and managing calendar events.
"""

from __future__ import annotations

import functools
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pincer.core.clock import load_zone
from pincer.integrations.google import calendar_writer
from pincer.integrations.google.calendar_render import (
    CALENDAR_ZONE_LABEL,
    CONFIGURED_ZONE_LABEL,
    EDIT_HINT,
    EVENT_ZONE_LABEL,
    event_instants,
    render_event_details,
    render_events,
    render_write,
    stored_zone_name,
    zones_equivalent,
)
from pincer.integrations.google.calendar_tz import (
    SOURCE_CALENDAR,
    clock_zone,
    is_date_only,
    local_day_span,
    localize,
    localize_bound,
    needs_zone,
    parse_iso,
    resolver_for,
    rfc3339,
)
from pincer.integrations.google.models import fmt_list
from pincer.integrations.google.quota import with_backoff

if TYPE_CHECKING:
    from collections.abc import Callable
    from zoneinfo import ZoneInfo

    from pincer.integrations.google.service_factory import GoogleServiceFactory
    from pincer.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

#: Google's ``sendUpdates`` values; anything else leaves the API default.
SEND_UPDATES_MODES = ("all", "externalOnly", "none")
KIND_CHANGE_MESSAGE = (
    "Moving an event between all-day and timed needs both a new start and a new end — "
    "pass end as well (a date for all-day, a time otherwise)."
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _days_after(bound: str, days: int) -> str:
    """``bound`` plus ``days`` (an instant), or now plus ``days`` if it can't be parsed."""
    parsed = parse_iso(bound)
    return ((parsed or datetime.now(UTC)) + timedelta(days=days)).isoformat()


def _user_timezone(context: dict[str, Any] | None) -> str:
    """The per-user identity zone the agent put in the tool context ("" if none)."""
    return str((context or {}).get("timezone") or "")


async def _calendar_zone(
    factory: GoogleServiceFactory, svc: Any, calendar_id: str, context: dict[str, Any] | None
) -> ZoneInfo:
    zone, _label = await _labeled_zone(factory, svc, calendar_id, context)
    return zone


def _range(time_min: str, time_max: str, zone: ZoneInfo | None, *, defaults: bool) -> tuple[str, str]:
    """A list/search/free-busy range as RFC 3339 with offsets (Google 400s offset-less ones).

    ``zone`` must be the resolved zone whenever a bound lacks an offset. With
    ``defaults`` (list, free/busy) the start is now and a missing end is the
    whole local day for a bare-date start, else 7 days after the start — never
    "now + 7 days", which can land before a future start and return nothing.
    Without ``defaults`` (search) a missing bound stays missing.
    """

    def _bound(value: str, *, end: bool = False) -> str:
        return localize_bound(value, zone, end=end) if zone else rfc3339(value)

    start = time_min or (_now_iso() if defaults else "")
    t_min = _bound(start) if start else ""
    if time_max:
        t_max = _bound(time_max, end=True)
    elif not defaults:
        t_max = ""
    elif zone is not None and is_date_only(start):
        t_max = localize_bound(start, zone, end=True)
    else:
        t_max = _days_after(t_min, 7)
    return t_min, t_max


def _clock(context: dict[str, Any] | None) -> ZoneInfo:
    """The agent clock zone for this user (for the renderer's mismatch note)."""
    return clock_zone(_user_timezone(context))


async def _labeled_zone(
    factory: GoogleServiceFactory, svc: Any, calendar_id: str, context: dict[str, Any] | None
) -> tuple[ZoneInfo, str]:
    """The resolved zone plus what it is — the calendar's own, or the configured fallback."""
    zone, source = await resolver_for(factory).resolve(svc, calendar_id, _user_timezone(context))
    return zone, CALENDAR_ZONE_LABEL if source == SOURCE_CALENDAR else CONFIGURED_ZONE_LABEL


# ── Tool implementations ──────────────────────────────────────────────────────


async def google__list_calendars(factory: GoogleServiceFactory) -> str:
    """List all calendars in the account."""
    svc = await factory.get("calendar")
    result = await with_backoff(lambda: svc.calendarList().list().execute())
    cals = result.get("items", [])
    if not cals:
        return "No calendars found."
    lines = [f"  {c.get('summary', '?')} (id={c['id']}, tz={c.get('timeZone', '?')})" for c in cals]
    return fmt_list(lines, header=f"{len(lines)} calendar(s):")


@dataclass(frozen=True)
class EventPage:
    """Events of one list call, with the zone and RFC 3339 range they were read in."""

    events: list[dict[str, Any]]
    zone: ZoneInfo
    zone_label: str
    time_min: str
    time_max: str
    more: bool


async def fetch_events(
    factory: GoogleServiceFactory,
    calendar_id: str = "primary",
    time_min: str = "",
    time_max: str = "",
    max_results: int = 50,
    user_timezone: str = "",
) -> EventPage:
    """Structured events in a range — the one list path behind the list tool, briefing and reminder.

    Bounds follow ``_range`` with defaults (offset-less ones read in the
    calendar's zone; a bare date alone is that whole local day).
    """
    svc = await factory.get("calendar")
    zone, label = await _labeled_zone(factory, svc, calendar_id, {"timezone": user_timezone})
    t_min, t_max = _range(time_min, time_max, zone, defaults=True)
    result = await with_backoff(
        lambda: (
            svc.events()
            .list(
                calendarId=calendar_id,
                timeMin=t_min,
                timeMax=t_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=max_results,
            )
            .execute()
        )
    )
    return EventPage(
        events=result.get("items", []),
        zone=zone,
        zone_label=label,
        time_min=t_min,
        time_max=t_max,
        more=bool(result.get("nextPageToken")),
    )


async def calendar_today(
    factory: GoogleServiceFactory,
    calendar_id: str = "primary",
    user_timezone: str = "",
    now: datetime | None = None,
) -> date:
    """Today's date in the calendar's own zone (not the deployment clock's)."""
    svc = await factory.get("calendar")
    zone, _label = await _labeled_zone(factory, svc, calendar_id, {"timezone": user_timezone})
    return (now or datetime.now(UTC)).astimezone(zone).date()


async def google__list_events(
    factory: GoogleServiceFactory,
    calendar_id: str = "primary",
    time_min: str = "",
    time_max: str = "",
    max_results: int = 50,
    context: dict[str, Any] | None = None,
) -> str:
    """List calendar events in a date range (default: now + 7 days; a bare date alone = that day)."""
    page = await fetch_events(factory, calendar_id, time_min, time_max, max_results, _user_timezone(context))
    first, last = local_day_span(page.time_min, page.time_max, page.zone)
    span = first if first == last else f"{first} – {last}"
    if not page.events:
        return f"No events on {first}." if first == last else f"No events between {first} and {last}."
    listed = render_events(
        page.events,
        page.zone,
        header=f"{len(page.events)} event(s) ({span})",
        clock_zone=_clock(context),
        zone_label=page.zone_label,
        more=page.more,
    )
    return f"{listed}\n{EDIT_HINT}"


async def google__get_event(
    factory: GoogleServiceFactory,
    event_id: str,
    calendar_id: str = "primary",
    context: dict[str, Any] | None = None,
) -> str:
    """Get full event details, times in the calendar's zone."""
    svc = await factory.get("calendar")
    event = await with_backoff(lambda: svc.events().get(calendarId=calendar_id, eventId=event_id).execute())
    zone, label = await _labeled_zone(factory, svc, calendar_id, context)
    details = render_event_details(event, zone, clock_zone=_clock(context), zone_label=label)
    return f"{details}\n{EDIT_HINT}"


async def google__search_events(
    factory: GoogleServiceFactory,
    query: str,
    calendar_id: str = "primary",
    max_results: int = 20,
    time_min: str = "",
    time_max: str = "",
    context: dict[str, Any] | None = None,
) -> str:
    """Search calendar events by text query, optionally within a date range."""
    svc = await factory.get("calendar")
    zone, label = await _labeled_zone(factory, svc, calendar_id, context)
    t_min, t_max = _range(time_min, time_max, zone, defaults=False)
    kw: dict[str, Any] = {
        "calendarId": calendar_id,
        "q": query,
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": max_results,
    }
    if t_min:
        kw["timeMin"] = t_min
    if t_max:
        kw["timeMax"] = t_max
    result = await with_backoff(lambda: svc.events().list(**kw).execute())
    events = result.get("items", [])
    if not events:
        return f"No events found for: {query}"
    found = render_events(
        events, zone, header=f"Found {len(events)} event(s)", clock_zone=_clock(context), zone_label=label
    )
    return f"{found}\n{EDIT_HINT}"


async def google__check_freebusy(
    factory: GoogleServiceFactory,
    emails: str,
    time_min: str = "",
    time_max: str = "",
    context: dict[str, Any] | None = None,
) -> str:
    """Check free/busy status for one or more email addresses.

    Offset-less bounds are read in the user's own (primary) calendar zone.
    """
    svc = await factory.get("calendar")
    zone = (
        await _calendar_zone(factory, svc, "primary", context) if needs_zone(time_min) or needs_zone(time_max) else None
    )
    t_min, t_max = _range(time_min, time_max, zone, defaults=True)
    items = [{"id": e.strip()} for e in emails.split(",") if e.strip()]
    body: dict[str, Any] = {"timeMin": t_min, "timeMax": t_max, "items": items}
    result = await with_backoff(lambda: svc.freebusy().query(body=body).execute())
    calendars = result.get("calendars", {})
    lines = []
    for cal_id, cal_data in calendars.items():
        busy_slots = cal_data.get("busy", [])
        if busy_slots:
            slot_strs = [f"    {s['start']} → {s['end']}" for s in busy_slots]
            lines.append(f"{cal_id}: BUSY at:")
            lines.extend(slot_strs)
        else:
            lines.append(f"{cal_id}: FREE")
    return "\n".join(lines) if lines else "No free/busy data returned."


async def _write_zone(
    factory: GoogleServiceFactory,
    svc: Any,
    calendar_id: str,
    context: dict[str, Any] | None,
    given: str,
) -> tuple[str, ZoneInfo | None]:
    """The ``timeZone`` to send with a start/end, and the zone to localize in.

    A given zone name (the caller's, or the event's stored one on update) wins;
    otherwise the resolver decides. An unknown given name is still sent as is
    (Google reports it) but cannot localize anything.
    """
    if given.strip():
        return given.strip(), load_zone(given)
    zone = await _calendar_zone(factory, svc, calendar_id, context)
    return zone.key, zone


async def _confirmation_zone(
    factory: GoogleServiceFactory,
    svc: Any,
    calendar_id: str,
    context: dict[str, Any] | None,
    saved: dict[str, Any],
    given: str,
) -> tuple[ZoneInfo, str]:
    """The zone a write confirmation shows, taken from Google's STORED copy.

    The event's own stored ``timeZone`` is shown (labelled as the event's)
    when it differs from the calendar's; otherwise the calendar's zone is. When
    the caller set a zone and Google stored exactly that one, it is the event's
    zone without a calendar lookup (the voice booking path).
    """
    own = load_zone(stored_zone_name(saved))
    given_zone = load_zone(given) if given.strip() else None
    if own is not None and given_zone is not None and own.key == given_zone.key:
        return own, EVENT_ZONE_LABEL
    zone, label = await _labeled_zone(factory, svc, calendar_id, context)
    if own is not None and not zones_equivalent(own, zone, event_instants([saved])):
        return own, EVENT_ZONE_LABEL
    return zone, label


async def google__create_event(
    factory: GoogleServiceFactory,
    summary: str,
    start: str,
    end: str,
    calendar_id: str = "primary",
    description: str = "",
    location: str = "",
    attendees: str = "",
    timezone: str = "",
    add_meet_link: bool = False,
    meet_link: str = "",
    send_updates: str = "",
    idempotency_key: str = "",
    context: dict[str, Any] | None = None,
) -> str:
    """Create a new calendar event, optionally with a Google Meet link.

    send_updates: 'all' | 'externalOnly' | 'none' — whether Google emails
        invitations to attendees (empty = API default, none).
    idempotency_key: replaces the chat turn's ID as the dedup seed of the
        client-supplied event ID, so the same key with the same details finds
        the existing event across turns (safe retries; see calendar_writer).
        Also stored as the private extended property ``pincer_key``.

    Offset-less start/end are read in ``timezone`` when given, else in the
    calendar's own zone (see calendar_tz); offset-bearing ones pass unchanged.
    """
    svc = await factory.get("calendar")

    tz_name, zone = await _write_zone(factory, svc, calendar_id, context, timezone)
    body: dict[str, Any] = {
        "summary": summary,
        "start": {"dateTime": localize(start, zone) if zone else rfc3339(start), "timeZone": tz_name},
        "end": {"dateTime": localize(end, zone) if zone else rfc3339(end), "timeZone": tz_name},
    }
    if idempotency_key:
        # Not used for dedup any more (the client-supplied ID is); kept as a
        # trace marker linking the event to the booking task in Google's UI/API.
        body["extendedProperties"] = {"private": {"pincer_key": idempotency_key}}
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    elif meet_link:
        # Embed the existing Meet link as the location so it shows in calendar
        body["location"] = meet_link
    if attendees:
        body["attendees"] = [{"email": e.strip()} for e in attendees.split(",") if e.strip()]

    conf_data_version = 0
    if add_meet_link and not meet_link:
        # Ask Calendar API to create a new Meet conference
        body["conferenceData"] = {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
        conf_data_version = 1

    insert_kw: dict[str, Any] = {}
    if conf_data_version:
        insert_kw["conferenceDataVersion"] = conf_data_version
    send_mode = send_updates.strip()
    if send_mode in SEND_UPDATES_MODES:
        insert_kw["sendUpdates"] = send_mode

    outcome = await calendar_writer.create(
        svc,
        calendar_id,
        body,
        seed=_dedup_seed(idempotency_key, context),
        trace=calendar_writer.Trace.from_context(context),
        **insert_kw,
    )
    created = outcome.event

    extra = ""
    if meet_link:
        extra = f"Meet link: {meet_link}"
    elif add_meet_link:
        uri = created.get("conferenceData", {}).get("entryPoints", [{}])[0].get("uri", "")
        if uri:
            extra = f"Meet link: {uri}"

    shown, label = await _confirmation_zone(factory, svc, calendar_id, context, created, timezone)
    title = created.get("summary", summary)
    headline = (
        f"Event already created: '{title}' (it already exists — not duplicated)"
        if outcome.existed
        else f"Event created: '{title}'"
    )
    return render_write(
        headline,
        created,
        shown,
        clock_zone=_clock(context),
        zone_label=label,
        extra=extra,
    )


def _dedup_seed(idempotency_key: str, context: dict[str, Any] | None) -> str:
    """The create dedup seed: the explicit key, else this chat turn's ID. Outside any
    turn (no context) a fresh seed still keeps the retry wrapper's resends safe."""
    return idempotency_key or str((context or {}).get("turn_id") or "") or uuid.uuid4().hex


def _end_keeping_duration(
    old_start: dict[str, Any], old_end: dict[str, Any], new_start: dict[str, Any], caller_zone: str = ""
) -> dict[str, Any]:
    """The end that keeps the event's stored duration after its start moves.

    Without this, "move it to 17:00" on a 10:00–11:00 event would send
    17:00–11:00, which Google rejects as an empty range. The duration is kept
    in WALL-CLOCK time of the start's zone (Fri 09:00 → Mon 09:00 stays
    "Monday 09:00" across a DST change, as in Google's own UI), and the end
    keeps its own stored zone (a Kyiv → New York flight still lands in New
    York time) unless the caller named a zone for this update. A wall-clock
    end that falls in the repeated fall-back hour takes its first occurrence
    (fold=0, still summer time); one in the skipped spring-forward hour shifts
    forward by the gap, as calendar_tz.localize does.
    """
    if "date" in new_start:
        if not (old_start.get("date") and old_end.get("date")):
            raise ValueError(KIND_CHANGE_MESSAGE)
        days = date.fromisoformat(old_end["date"]) - date.fromisoformat(old_start["date"])
        return {"date": (date.fromisoformat(new_start["date"]) + days).isoformat()}
    if not (old_start.get("dateTime") and old_end.get("dateTime")):
        raise ValueError(KIND_CHANGE_MESSAGE)
    was_start, was_end = parse_iso(old_start["dateTime"]), parse_iso(old_end["dateTime"])
    starts = parse_iso(new_start["dateTime"])
    if was_start is None or was_end is None or starts is None or starts.tzinfo is None:
        raise ValueError(f"Could not read the new start {new_start.get('dateTime')!r}; pass end as well.")
    start_zone = load_zone(stored_zone_name({"start": new_start}))
    if start_zone is not None:
        wall = was_end.astimezone(start_zone).replace(tzinfo=None) - was_start.astimezone(start_zone).replace(
            tzinfo=None
        )
        moved = (starts.astimezone(start_zone).replace(tzinfo=None) + wall).replace(tzinfo=start_zone)
    else:
        moved = starts + (was_end - was_start)
    end_name = (
        caller_zone.strip() or stored_zone_name({"end": old_end}, "end") or stored_zone_name({"start": new_start})
    )
    end_zone = load_zone(end_name) if end_name else None
    end: dict[str, Any] = {"dateTime": (moved.astimezone(end_zone) if end_zone else moved).isoformat()}
    if end_name:
        end["timeZone"] = end_name
    return end


async def google__update_event(
    factory: GoogleServiceFactory,
    event_id: str,
    calendar_id: str = "primary",
    summary: str = "",
    start: str = "",
    end: str = "",
    description: str = "",
    location: str = "",
    timezone: str = "",
    context: dict[str, Any] | None = None,
) -> str:
    """Update fields on an existing calendar event.

    Offset-less start/end are read in ``timezone`` when given, else in the
    event's own stored zone, else in the calendar's zone. A bare date moves an
    all-day event. Given only a new start, the end moves with it (the stored
    duration is kept); given only a new end, the start stays.
    """
    svc = await factory.get("calendar")
    event = await with_backoff(lambda: svc.events().get(calendarId=calendar_id, eventId=event_id).execute())
    if summary:
        event["summary"] = summary
    if description:
        event["description"] = description
    if location:
        event["location"] = location
    changed: dict[str, dict[str, Any]] = {}
    for key, value in (("start", start), ("end", end)):
        if not value:
            continue
        if is_date_only(value):
            changed[key] = {"date": value.strip()}
            continue
        stored_zone = stored_zone_name(event, key)
        tz_name, zone = await _write_zone(factory, svc, calendar_id, context, timezone or stored_zone)
        changed[key] = {"dateTime": localize(value, zone) if zone else rfc3339(value), "timeZone": tz_name}
    if "start" in changed and "end" not in changed:
        changed["end"] = _end_keeping_duration(
            event.get("start") or {}, event.get("end") or {}, changed["start"], timezone
        )
    event.update(changed)
    if ("date" in (event.get("start") or {})) != ("date" in (event.get("end") or {})):
        # A mixed all-day/timed body is a raw 400 from Google; say what to do instead.
        raise ValueError(KIND_CHANGE_MESSAGE)
    updated = await calendar_writer.update(
        svc, calendar_id, event_id, event, trace=calendar_writer.Trace.from_context(context)
    )
    shown, label = await _confirmation_zone(factory, svc, calendar_id, context, updated, timezone)
    return render_write(
        f"Event updated: '{updated.get('summary', '')}'",
        updated,
        shown,
        clock_zone=_clock(context),
        zone_label=label,
    )


async def google__delete_event(
    factory: GoogleServiceFactory,
    event_id: str,
    calendar_id: str = "primary",
    context: dict[str, Any] | None = None,
) -> str:
    """Delete (cancel) a calendar event."""
    svc = await factory.get("calendar")
    await calendar_writer.delete(svc, calendar_id, event_id, trace=calendar_writer.Trace.from_context(context))
    return f"Event {event_id} deleted."


async def google__move_event(
    factory: GoogleServiceFactory,
    event_id: str,
    destination_calendar_id: str,
    source_calendar_id: str = "primary",
    context: dict[str, Any] | None = None,
) -> str:
    """Move an event to a different calendar."""
    svc = await factory.get("calendar")
    result = await calendar_writer.move(
        svc,
        source_calendar_id,
        event_id,
        destination_calendar_id,
        trace=calendar_writer.Trace.from_context(context),
    )
    return f"Event {event_id} moved to {destination_calendar_id} (new id={result.get('id', '')})"


def _as_event(value: Any) -> dict[str, Any] | None:
    """A Google response as an event dict for logging (None if it isn't one)."""
    return value if isinstance(value, dict) else None


async def google__accept_event(
    factory: GoogleServiceFactory,
    event_id: str,
    calendar_id: str = "primary",
    context: dict[str, Any] | None = None,
) -> str:
    """Accept a calendar invitation."""
    svc = await factory.get("calendar")
    event = await with_backoff(lambda: svc.events().get(calendarId=calendar_id, eventId=event_id).execute())
    # Update self in attendees list
    profile = await with_backoff(lambda: svc.calendarList().get(calendarId=calendar_id).execute())
    self_email = profile.get("id", "")
    for att in event.get("attendees", []):
        if att.get("self") or att.get("email") == self_email:
            att["responseStatus"] = "accepted"
    patched = await with_backoff(
        lambda: (
            svc.events()
            .patch(calendarId=calendar_id, eventId=event_id, body={"attendees": event.get("attendees", [])})
            .execute()
        )
    )
    calendar_writer.log_write(
        "accept", calendar_writer.Trace.from_context(context), calendar_id, _as_event(patched), event_id=event_id
    )
    return f"Accepted event: {event.get('summary', event_id)}"


async def google__decline_event(
    factory: GoogleServiceFactory,
    event_id: str,
    calendar_id: str = "primary",
    context: dict[str, Any] | None = None,
) -> str:
    """Decline a calendar invitation."""
    svc = await factory.get("calendar")
    event = await with_backoff(lambda: svc.events().get(calendarId=calendar_id, eventId=event_id).execute())
    for att in event.get("attendees", []):
        if att.get("self"):
            att["responseStatus"] = "declined"
    patched = await with_backoff(
        lambda: (
            svc.events()
            .patch(calendarId=calendar_id, eventId=event_id, body={"attendees": event.get("attendees", [])})
            .execute()
        )
    )
    calendar_writer.log_write(
        "decline", calendar_writer.Trace.from_context(context), calendar_id, _as_event(patched), event_id=event_id
    )
    return f"Declined event: {event.get('summary', event_id)}"


async def google__add_google_meet(
    factory: GoogleServiceFactory,
    event_id: str,
    calendar_id: str = "primary",
    context: dict[str, Any] | None = None,
) -> str:
    """Add a Google Meet link to an existing event."""
    svc = await factory.get("calendar")
    body = {
        "conferenceData": {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
    }
    updated = await with_backoff(
        lambda: (
            svc.events()
            .patch(
                calendarId=calendar_id,
                eventId=event_id,
                body=body,
                conferenceDataVersion=1,
            )
            .execute()
        )
    )
    calendar_writer.log_write(
        "add_meet", calendar_writer.Trace.from_context(context), calendar_id, _as_event(updated), event_id=event_id
    )
    meet_link = updated.get("conferenceData", {}).get("entryPoints", [{}])[0].get("uri", "pending")
    return f"Google Meet added to event {event_id}: {meet_link}"


# ── Registry ──────────────────────────────────────────────────────────────────


def register_calendar_tools(registry: ToolRegistry, factory: GoogleServiceFactory) -> int:
    """Register all 12 Calendar tools. Returns count."""

    def _h(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def wrapper(**kwargs):  # type: ignore[no-untyped-def]
            return await fn(factory, **kwargs)

        return wrapper

    registry.register(
        name="google__list_calendars",
        description="List all Google Calendars in the account.",
        handler=_h(google__list_calendars),
        parameters={"type": "object", "properties": {}, "required": []},
    )
    registry.register(
        name="google__list_events",
        description=(
            "List calendar events in a date range (default: next 7 days). "
            "Times without a UTC offset are read in the calendar's timezone."
        ),
        handler=_h(google__list_events),
        parameters={
            "type": "object",
            "properties": {
                "calendar_id": {"type": "string", "default": "primary"},
                "time_min": {
                    "type": "string",
                    "description": "ISO 8601 start, or a date for that whole local day (default: now)",
                    "default": "",
                },
                "time_max": {
                    "type": "string",
                    "description": "ISO 8601 end, or a date to include that whole local day "
                    "(default: the start's whole day when the start is a bare date, else start + 7 days)",
                    "default": "",
                },
                "max_results": {"type": "integer", "default": 50},
            },
            "required": [],
        },
    )
    registry.register(
        name="google__get_event",
        description="Get full details of a calendar event by ID.",
        handler=_h(google__get_event),
        parameters={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "calendar_id": {"type": "string", "default": "primary"},
            },
            "required": ["event_id"],
        },
    )
    registry.register(
        name="google__search_events",
        description=(
            "Search calendar events by text (title, description, location), optionally within a date range. "
            "Times without a UTC offset are read in the calendar's timezone."
        ),
        handler=_h(google__search_events),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "calendar_id": {"type": "string", "default": "primary"},
                "max_results": {"type": "integer", "default": 20},
                "time_min": {
                    "type": "string",
                    "description": "Optional ISO 8601 start, or a date for that whole local day",
                    "default": "",
                },
                "time_max": {
                    "type": "string",
                    "description": "Optional ISO 8601 end, or a date to include that whole local day",
                    "default": "",
                },
            },
            "required": ["query"],
        },
    )
    registry.register(
        name="google__check_freebusy",
        description=(
            "Check free/busy status for one or more people by email (comma-separated). "
            "Times without a UTC offset are read in your primary calendar's timezone."
        ),
        handler=_h(google__check_freebusy),
        parameters={
            "type": "object",
            "properties": {
                "emails": {"type": "string", "description": "Comma-separated email addresses"},
                "time_min": {"type": "string", "description": "ISO 8601 start (default: now)", "default": ""},
                "time_max": {
                    "type": "string",
                    "description": "ISO 8601 end (default: the start's whole day when the start is a bare date, "
                    "else start + 7 days)",
                    "default": "",
                },
            },
            "required": ["emails"],
        },
    )
    registry.register(
        name="google__create_event",
        description=(
            "Create a new Google Calendar event with optional attendees, location, and description. "
            "Only for a new event that does not exist yet: never use it to change, rename or reschedule "
            "an existing event — call google__update_event with the event's ID for that (find the ID with "
            "google__search_events or google__list_events). "
            "Set add_meet_link=True to auto-create a new Google Meet conference. "
            "Or set meet_link to the URI of an existing Meet space."
        ),
        handler=_h(google__create_event),
        parameters={
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Event title"},
                "start": {
                    "type": "string",
                    "description": "Start time ISO 8601 as LOCAL wall-clock time (e.g. 2026-03-28T14:00:00); "
                    "without a UTC offset it is read in the calendar's timezone (or the timezone parameter)",
                },
                "end": {"type": "string", "description": "End time ISO 8601, same rules as start"},
                "calendar_id": {"type": "string", "default": "primary"},
                "description": {"type": "string", "default": ""},
                "location": {"type": "string", "default": ""},
                "attendees": {"type": "string", "description": "Comma-separated email addresses", "default": ""},
                "timezone": {
                    "type": "string",
                    "description": "IANA timezone for start/end when they carry no UTC offset "
                    "(default: the calendar's timezone). Leave empty unless the user names another zone",
                    "default": "",
                },
                "add_meet_link": {
                    "type": "boolean",
                    "description": "Auto-create a new Google Meet conference for this event",
                    "default": False,
                },
                "meet_link": {
                    "type": "string",
                    "description": "URI of an existing Meet space (e.g. https://meet.google.com/abc-defg-hij)",
                    "default": "",
                },
                "send_updates": {
                    "type": "string",
                    "description": "Email invitations to attendees: 'all' | 'externalOnly' | 'none' (default)",
                    "default": "",
                },
                "idempotency_key": {
                    "type": "string",
                    "description": "Optional dedup key for EXACT re-calls: the same key with the same calendar, "
                    "title, start and end returns the existing event instead of a duplicate (safe retries). "
                    "It does not match an event whose details differ — a changed time creates a new event",
                    "default": "",
                },
            },
            "required": ["summary", "start", "end"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__update_event",
        description=(
            "Edit an existing calendar event (meeting or appointment) by its ID: reschedule or move it to "
            "another time or day, rename it, change the time, or change details such as description or "
            "location. Only provided fields are changed and the event keeps its ID. Use this — not "
            "google__create_event — for any change to an event; without the ID, find it first with "
            "google__search_events. "
            "Times without a UTC offset are read in the event's own timezone, else the calendar's timezone."
        ),
        handler=_h(google__update_event),
        parameters={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "calendar_id": {"type": "string", "default": "primary"},
                "summary": {"type": "string", "default": ""},
                "start": {
                    "type": "string",
                    "description": "New start, ISO 8601 local wall-clock time (e.g. 2026-03-28T17:00:00), or a date "
                    "for an all-day event. Given without end, the end moves with it (duration kept)",
                    "default": "",
                },
                "end": {
                    "type": "string",
                    "description": "New end, same rules as start. Given without start, the start stays",
                    "default": "",
                },
                "description": {"type": "string", "default": ""},
                "location": {"type": "string", "default": ""},
                "timezone": {
                    "type": "string",
                    "description": "IANA timezone for offset-less start/end (default: the event's own, "
                    "else the calendar's timezone). Leave empty unless the user names another zone",
                    "default": "",
                },
            },
            "required": ["event_id"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__delete_event",
        description="Delete (cancel) a calendar event.",
        handler=_h(google__delete_event),
        parameters={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "calendar_id": {"type": "string", "default": "primary"},
            },
            "required": ["event_id"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__move_event",
        description=(
            "Move a calendar event to a different calendar. This is not for changing the time: "
            "to reschedule an event, use google__update_event."
        ),
        handler=_h(google__move_event),
        parameters={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "destination_calendar_id": {"type": "string"},
                "source_calendar_id": {"type": "string", "default": "primary"},
            },
            "required": ["event_id", "destination_calendar_id"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__accept_event",
        description="Accept a calendar invitation.",
        handler=_h(google__accept_event),
        parameters={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "calendar_id": {"type": "string", "default": "primary"},
            },
            "required": ["event_id"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__decline_event",
        description="Decline a calendar invitation.",
        handler=_h(google__decline_event),
        parameters={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "calendar_id": {"type": "string", "default": "primary"},
            },
            "required": ["event_id"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__add_google_meet",
        description="Add a Google Meet video conference link to an existing calendar event.",
        handler=_h(google__add_google_meet),
        parameters={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "calendar_id": {"type": "string", "default": "primary"},
            },
            "required": ["event_id"],
        },
        require_approval=True,
    )
    return 12
