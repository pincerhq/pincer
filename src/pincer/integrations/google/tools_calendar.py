"""
Google Calendar tools — 12 tools for reading and managing calendar events.
"""

from __future__ import annotations

import functools
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable

from pincer.integrations.google.models import fmt_event, fmt_list
from pincer.integrations.google.quota import with_backoff

if TYPE_CHECKING:
    from pincer.integrations.google.service_factory import GoogleServiceFactory
    from pincer.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _week_end_iso() -> str:
    return (datetime.now(UTC) + timedelta(days=7)).isoformat()


# ── Tool implementations ──────────────────────────────────────────────────────

async def google__list_calendars(factory: "GoogleServiceFactory") -> str:
    """List all calendars in the account."""
    svc = await factory.get("calendar")
    result = await with_backoff(lambda: svc.calendarList().list().execute())
    cals = result.get("items", [])
    if not cals:
        return "No calendars found."
    lines = [f"  {c.get('summary', '?')} (id={c['id']}, tz={c.get('timeZone', '?')})" for c in cals]
    return fmt_list(lines, header=f"{len(lines)} calendar(s):")


async def google__list_events(
    factory: "GoogleServiceFactory",
    calendar_id: str = "primary",
    time_min: str = "",
    time_max: str = "",
    max_results: int = 50,
) -> str:
    """List calendar events in a date range (default: today + 7 days)."""
    svc = await factory.get("calendar")
    t_min = time_min or _now_iso()
    t_max = time_max or _week_end_iso()
    result = await with_backoff(
        lambda: svc.events().list(
            calendarId=calendar_id,
            timeMin=t_min,
            timeMax=t_max,
            singleEvents=True,
            orderBy="startTime",
            maxResults=max_results,
        ).execute()
    )
    events = result.get("items", [])
    if not events:
        return f"No events between {t_min[:10]} and {t_max[:10]}."
    lines = [fmt_event(e) for e in events]
    more = bool(result.get("nextPageToken"))
    return fmt_list(lines, header=f"{len(lines)} event(s) ({t_min[:10]} – {t_max[:10]}):", more=more)


async def google__get_event(
    factory: "GoogleServiceFactory",
    event_id: str,
    calendar_id: str = "primary",
) -> str:
    """Get full event details."""
    svc = await factory.get("calendar")
    event = await with_backoff(
        lambda: svc.events().get(calendarId=calendar_id, eventId=event_id).execute()
    )
    lines = [
        f"Title:       {event.get('summary', '(no title)')}",
        f"Start:       {event.get('start', {}).get('dateTime', event.get('start', {}).get('date', ''))}",
        f"End:         {event.get('end', {}).get('dateTime', event.get('end', {}).get('date', ''))}",
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


async def google__search_events(
    factory: "GoogleServiceFactory",
    query: str,
    calendar_id: str = "primary",
    max_results: int = 20,
) -> str:
    """Search calendar events by text query."""
    svc = await factory.get("calendar")
    result = await with_backoff(
        lambda: svc.events().list(
            calendarId=calendar_id,
            q=query,
            singleEvents=True,
            orderBy="startTime",
            maxResults=max_results,
        ).execute()
    )
    events = result.get("items", [])
    if not events:
        return f"No events found for: {query}"
    lines = [fmt_event(e) for e in events]
    return fmt_list(lines, header=f"Found {len(lines)} event(s):")


async def google__check_freebusy(
    factory: "GoogleServiceFactory",
    emails: str,
    time_min: str = "",
    time_max: str = "",
) -> str:
    """Check free/busy status for one or more email addresses."""
    svc = await factory.get("calendar")
    t_min = time_min or _now_iso()
    t_max = time_max or _week_end_iso()
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


async def google__create_event(
    factory: "GoogleServiceFactory",
    summary: str,
    start: str,
    end: str,
    calendar_id: str = "primary",
    description: str = "",
    location: str = "",
    attendees: str = "",
    timezone: str = "UTC",
    add_meet_link: bool = False,
    meet_link: str = "",
) -> str:
    """Create a new calendar event, optionally with a Google Meet link."""
    import uuid

    svc = await factory.get("calendar")
    body: dict[str, Any] = {
        "summary": summary,
        "start": {"dateTime": start, "timeZone": timezone},
        "end": {"dateTime": end, "timeZone": timezone},
    }
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

    _cal = calendar_id
    _body = body
    _cdv = conf_data_version

    def _insert() -> dict[str, Any]:
        kw: dict[str, Any] = {"calendarId": _cal, "body": _body}
        if _cdv:
            kw["conferenceDataVersion"] = _cdv
        return svc.events().insert(**kw).execute()  # type: ignore[no-any-return]

    created = await with_backoff(_insert)

    extra = ""
    if meet_link:
        extra = f"\nMeet link: {meet_link}"
    elif add_meet_link:
        uri = (
            created.get("conferenceData", {})
            .get("entryPoints", [{}])[0]
            .get("uri", "")
        )
        if uri:
            extra = f"\nMeet link: {uri}"

    return (
        f"Event created: '{summary}'\n"
        f"Start: {start} | End: {end}\n"
        f"ID: {created.get('id', '')}\n"
        f"Link: {created.get('htmlLink', '')}"
        f"{extra}"
    )


async def google__update_event(
    factory: "GoogleServiceFactory",
    event_id: str,
    calendar_id: str = "primary",
    summary: str = "",
    start: str = "",
    end: str = "",
    description: str = "",
    location: str = "",
    timezone: str = "",
) -> str:
    """Update fields on an existing calendar event."""
    svc = await factory.get("calendar")
    event = await with_backoff(
        lambda: svc.events().get(calendarId=calendar_id, eventId=event_id).execute()
    )
    if summary:
        event["summary"] = summary
    if description:
        event["description"] = description
    if location:
        event["location"] = location
    if start:
        tz = timezone or event.get("start", {}).get("timeZone", "UTC")
        event["start"] = {"dateTime": start, "timeZone": tz}
    if end:
        tz = timezone or event.get("end", {}).get("timeZone", "UTC")
        event["end"] = {"dateTime": end, "timeZone": tz}
    updated = await with_backoff(
        lambda: svc.events().update(calendarId=calendar_id, eventId=event_id, body=event).execute()
    )
    return f"Event updated: '{updated.get('summary', '')}' (id={event_id})"


async def google__delete_event(
    factory: "GoogleServiceFactory",
    event_id: str,
    calendar_id: str = "primary",
) -> str:
    """Delete (cancel) a calendar event."""
    svc = await factory.get("calendar")
    await with_backoff(lambda: svc.events().delete(calendarId=calendar_id, eventId=event_id).execute())
    return f"Event {event_id} deleted."


async def google__move_event(
    factory: "GoogleServiceFactory",
    event_id: str,
    destination_calendar_id: str,
    source_calendar_id: str = "primary",
) -> str:
    """Move an event to a different calendar."""
    svc = await factory.get("calendar")
    result = await with_backoff(
        lambda: svc.events().move(
            calendarId=source_calendar_id,
            eventId=event_id,
            destination=destination_calendar_id,
        ).execute()
    )
    return f"Event {event_id} moved to {destination_calendar_id} (new id={result.get('id', '')})"


async def google__accept_event(
    factory: "GoogleServiceFactory",
    event_id: str,
    calendar_id: str = "primary",
) -> str:
    """Accept a calendar invitation."""
    svc = await factory.get("calendar")
    event = await with_backoff(
        lambda: svc.events().get(calendarId=calendar_id, eventId=event_id).execute()
    )
    # Update self in attendees list
    profile = await with_backoff(lambda: svc.calendarList().get(calendarId=calendar_id).execute())
    self_email = profile.get("id", "")
    for att in event.get("attendees", []):
        if att.get("self") or att.get("email") == self_email:
            att["responseStatus"] = "accepted"
    await with_backoff(
        lambda: svc.events().patch(
            calendarId=calendar_id, eventId=event_id,
            body={"attendees": event.get("attendees", [])}
        ).execute()
    )
    return f"Accepted event: {event.get('summary', event_id)}"


async def google__decline_event(
    factory: "GoogleServiceFactory",
    event_id: str,
    calendar_id: str = "primary",
) -> str:
    """Decline a calendar invitation."""
    svc = await factory.get("calendar")
    event = await with_backoff(
        lambda: svc.events().get(calendarId=calendar_id, eventId=event_id).execute()
    )
    for att in event.get("attendees", []):
        if att.get("self"):
            att["responseStatus"] = "declined"
    await with_backoff(
        lambda: svc.events().patch(
            calendarId=calendar_id, eventId=event_id,
            body={"attendees": event.get("attendees", [])}
        ).execute()
    )
    return f"Declined event: {event.get('summary', event_id)}"


async def google__add_google_meet(
    factory: "GoogleServiceFactory",
    event_id: str,
    calendar_id: str = "primary",
) -> str:
    """Add a Google Meet link to an existing event."""
    import uuid
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
        lambda: svc.events().patch(
            calendarId=calendar_id,
            eventId=event_id,
            body=body,
            conferenceDataVersion=1,
        ).execute()
    )
    meet_link = (
        updated.get("conferenceData", {})
        .get("entryPoints", [{}])[0]
        .get("uri", "pending")
    )
    return f"Google Meet added to event {event_id}: {meet_link}"


# ── Registry ──────────────────────────────────────────────────────────────────

def register_calendar_tools(registry: "ToolRegistry", factory: "GoogleServiceFactory") -> int:
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
        description="List calendar events in a date range (default: next 7 days).",
        handler=_h(google__list_events),
        parameters={
            "type": "object",
            "properties": {
                "calendar_id": {"type": "string", "default": "primary"},
                "time_min": {"type": "string", "description": "ISO 8601 start (default: now)", "default": ""},
                "time_max": {"type": "string", "description": "ISO 8601 end (default: +7d)", "default": ""},
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
        description="Search calendar events by text (title, description, location).",
        handler=_h(google__search_events),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "calendar_id": {"type": "string", "default": "primary"},
                "max_results": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
    )
    registry.register(
        name="google__check_freebusy",
        description="Check free/busy status for one or more people by email (comma-separated).",
        handler=_h(google__check_freebusy),
        parameters={
            "type": "object",
            "properties": {
                "emails": {"type": "string", "description": "Comma-separated email addresses"},
                "time_min": {"type": "string", "description": "ISO 8601 start (default: now)", "default": ""},
                "time_max": {"type": "string", "description": "ISO 8601 end (default: +7d)", "default": ""},
            },
            "required": ["emails"],
        },
    )
    registry.register(
        name="google__create_event",
        description=(
            "Create a new Google Calendar event with optional attendees, location, and description. "
            "Set add_meet_link=True to auto-create a new Google Meet conference. "
            "Or set meet_link to the URI of an existing Meet space."
        ),
        handler=_h(google__create_event),
        parameters={
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Event title"},
                "start": {"type": "string", "description": "Start time ISO 8601 (e.g. 2026-03-28T14:00:00)"},
                "end": {"type": "string", "description": "End time ISO 8601"},
                "calendar_id": {"type": "string", "default": "primary"},
                "description": {"type": "string", "default": ""},
                "location": {"type": "string", "default": ""},
                "attendees": {"type": "string", "description": "Comma-separated email addresses", "default": ""},
                "timezone": {"type": "string", "description": "IANA timezone (default: UTC)", "default": "UTC"},
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
            },
            "required": ["summary", "start", "end"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__update_event",
        description="Update fields on an existing calendar event. Only provided fields are changed.",
        handler=_h(google__update_event),
        parameters={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "calendar_id": {"type": "string", "default": "primary"},
                "summary": {"type": "string", "default": ""},
                "start": {"type": "string", "default": ""},
                "end": {"type": "string", "default": ""},
                "description": {"type": "string", "default": ""},
                "location": {"type": "string", "default": ""},
                "timezone": {"type": "string", "default": ""},
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
        description="Move a calendar event to a different calendar.",
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
