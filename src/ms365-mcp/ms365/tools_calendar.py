"""
Microsoft 365 Calendar tools — 12 tools for reading and managing calendar events.
"""

from __future__ import annotations

import functools
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from ms365._registry import ToolRegistry
    from ms365.graph_client import GraphClient

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.0000000")


def _week_end_iso() -> str:
    return (datetime.now(UTC) + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S.0000000")


def _fmt_event(e: dict[str, Any]) -> str:
    """Format a single calendar event for display."""
    start = e.get("start", {})
    end = e.get("end", {})
    start_str = start.get("dateTime", start.get("date", ""))
    end_str = end.get("dateTime", end.get("date", ""))
    organizer = e.get("organizer", {}).get("emailAddress", {}).get("name", "")

    lines = [
        f"  {e.get('subject', '(no title)')}",
        f"    Start: {start_str}",
        f"    End: {end_str}",
    ]
    if organizer:
        lines.append(f"    Organizer: {organizer}")
    if e.get("location", {}).get("displayName"):
        lines.append(f"    Location: {e['location']['displayName']}")
    if e.get("isOnlineMeeting"):
        join_url = e.get("onlineMeeting", {}).get("joinUrl", "")
        if join_url:
            lines.append(f"    Join: {join_url}")
    lines.append(f"    ID: {e.get('id', '')}")
    return "\n".join(lines)


# ── Tool implementations ──────────────────────────────────────────────────────


async def outlook__list_calendars(client: GraphClient) -> str:
    """List all calendars in the account."""
    data = await client.get("/me/calendars")
    cals = data.get("value", [])
    if not cals:
        return "No calendars found."
    lines = [f"  {c.get('name', '?')} (id={c.get('id', '')}, color={c.get('color', '?')})" for c in cals]
    return f"{len(lines)} calendar(s):\n" + "\n".join(lines)


async def outlook__list_events(
    client: GraphClient,
    calendar_id: str = "",
    time_min: str = "",
    time_max: str = "",
    max_results: int = 50,
) -> str:
    """List calendar events in a date range (default: next 7 days)."""
    t_min = time_min or _now_iso()
    t_max = time_max or _week_end_iso()

    path = "/me/calendarView"
    params: dict[str, Any] = {
        "startDateTime": t_min,
        "endDateTime": t_max,
        "$top": str(max_results),
        "$orderby": "start/dateTime",
        "$select": "id,subject,start,end,organizer,location,isOnlineMeeting,onlineMeeting",
    }

    data = await client.get(path, params=params)
    events = data.get("value", [])
    if not events:
        return f"No events between {t_min[:10]} and {t_max[:10]}."
    lines = [_fmt_event(e) for e in events]
    more = bool(data.get("@odata.nextLink"))
    header = f"{len(lines)} event(s) ({t_min[:10]} — {t_max[:10]}):"
    if more:
        header += f" (showing first {max_results}, more available)"
    return header + "\n" + "\n\n".join(lines)


async def outlook__get_event(
    client: GraphClient,
    event_id: str,
) -> str:
    """Get full event details by ID."""
    event = await client.get(f"/me/events/{event_id}")
    start = event.get("start", {})
    end = event.get("end", {})
    organizer = event.get("organizer", {}).get("emailAddress", {})

    lines = [
        f"Title:       {event.get('subject', '(no title)')}",
        f"Start:       {start.get('dateTime', '')} ({start.get('timeZone', '')})",
        f"End:         {end.get('dateTime', '')} ({end.get('timeZone', '')})",
        f"Location:    {event.get('location', {}).get('displayName', '')}",
        f"Description: {(event.get('body', {}).get('content', ''))[:500]}",
        f"Status:      {event.get('showAs', '')}",
        f"Organizer:   {organizer.get('name', '')} <{organizer.get('address', '')}>",
        f"Online:      {event.get('isOnlineMeeting', False)}",
        f"ID:          {event.get('id', '')}",
    ]
    if event.get("onlineMeeting", {}).get("joinUrl"):
        lines.append(f"Join URL:    {event['onlineMeeting']['joinUrl']}")

    attendees = event.get("attendees", [])
    if attendees:
        lines.append(f"Attendees ({len(attendees)}):")
        for att in attendees:
            email = att.get("emailAddress", {})
            status = att.get("status", {}).get("response", "?")
            lines.append(f"  {email.get('name', '')} <{email.get('address', '')}> [{status}]")
    return "\n".join(lines)


async def outlook__search_events(
    client: GraphClient,
    query: str,
    max_results: int = 20,
) -> str:
    """Search calendar events by subject text."""
    params: dict[str, Any] = {
        "$filter": f"contains(subject, '{query}')",
        "$top": str(max_results),
        "$orderby": "start/dateTime desc",
        "$select": "id,subject,start,end,organizer,location",
    }
    data = await client.get("/me/events", params=params)
    events = data.get("value", [])
    if not events:
        return f"No events matching '{query}'."
    lines = [_fmt_event(e) for e in events]
    return f"Search results for '{query}' ({len(lines)} found):\n" + "\n\n".join(lines)


async def outlook__check_availability(
    client: GraphClient,
    emails: str,
    time_min: str = "",
    time_max: str = "",
) -> str:
    """Check free/busy status for one or more people."""
    t_min = time_min or _now_iso()
    t_max = time_max or _week_end_iso()

    schedules = [e.strip() for e in emails.split(",") if e.strip()]
    body = {
        "schedules": schedules,
        "startTime": {"dateTime": t_min, "timeZone": "UTC"},
        "endTime": {"dateTime": t_max, "timeZone": "UTC"},
        "availabilityViewInterval": 30,
    }
    data = await client.post("/me/calendar/getSchedule", json=body)
    results = data.get("value", [])

    lines = []
    for sched in results:
        email = sched.get("scheduleId", "?")
        items = sched.get("scheduleItems", [])
        if not items:
            lines.append(f"  {email}: Free for entire period")
        else:
            lines.append(f"  {email}:")
            for item in items:
                start_dt = item.get("start", {}).get("dateTime", "")
                end_dt = item.get("end", {}).get("dateTime", "")
                status = item.get("status", "?")
                subj = item.get("subject", "")
                lines.append(f"    {start_dt} — {end_dt}: {status} ({subj})")

    return f"Availability ({t_min[:10]} — {t_max[:10]}):\n" + "\n".join(lines)


async def outlook__create_event(
    client: GraphClient,
    subject: str,
    start: str,
    end: str,
    description: str = "",
    location: str = "",
    attendees: str = "",
    timezone: str = "UTC",
    is_online_meeting: bool = False,
) -> str:
    """Create a new calendar event."""
    body: dict[str, Any] = {
        "subject": subject,
        "start": {"dateTime": start, "timeZone": timezone},
        "end": {"dateTime": end, "timeZone": timezone},
    }
    if description:
        body["body"] = {"contentType": "Text", "content": description}
    if location:
        body["location"] = {"displayName": location}
    if attendees:
        body["attendees"] = [
            {
                "emailAddress": {"address": addr.strip()},
                "type": "required",
            }
            for addr in attendees.split(",")
            if addr.strip()
        ]
    if is_online_meeting:
        body["isOnlineMeeting"] = True
        body["onlineMeetingProvider"] = "teamsForBusiness"

    result = await client.post("/me/events", json=body)
    extra = ""
    if (result.get("onlineMeeting") or {}).get("joinUrl"):
        extra = f"\nTeams link: {result['onlineMeeting']['joinUrl']}"
    return f"Event created: '{subject}'\nStart: {start} | End: {end}\nID: {result.get('id', '')}{extra}"


async def outlook__update_event(
    client: GraphClient,
    event_id: str,
    subject: str = "",
    start: str = "",
    end: str = "",
    description: str = "",
    location: str = "",
    timezone: str = "",
) -> str:
    """Update fields on an existing calendar event."""
    updates: dict[str, Any] = {}
    if subject:
        updates["subject"] = subject
    if description:
        updates["body"] = {"contentType": "Text", "content": description}
    if location:
        updates["location"] = {"displayName": location}
    if start:
        updates["start"] = {"dateTime": start, "timeZone": timezone or "UTC"}
    if end:
        updates["end"] = {"dateTime": end, "timeZone": timezone or "UTC"}

    await client.patch(f"/me/events/{event_id}", json=updates)
    return f"Event {event_id} updated."


async def outlook__delete_event(
    client: GraphClient,
    event_id: str,
) -> str:
    """Delete (cancel) a calendar event."""
    await client.delete(f"/me/events/{event_id}")
    return f"Event {event_id} deleted."


async def outlook__accept_event(
    client: GraphClient,
    event_id: str,
    comment: str = "",
) -> str:
    """Accept a meeting invitation."""
    body: dict[str, Any] = {"sendResponse": True}
    if comment:
        body["comment"] = comment
    await client.post(f"/me/events/{event_id}/accept", json=body)
    return f"Event {event_id} accepted."


async def outlook__decline_event(
    client: GraphClient,
    event_id: str,
    comment: str = "",
) -> str:
    """Decline a meeting invitation."""
    body: dict[str, Any] = {"sendResponse": True}
    if comment:
        body["comment"] = comment
    await client.post(f"/me/events/{event_id}/decline", json=body)
    return f"Event {event_id} declined."


async def outlook__tentative_event(
    client: GraphClient,
    event_id: str,
    comment: str = "",
) -> str:
    """Tentatively accept a meeting invitation."""
    body: dict[str, Any] = {"sendResponse": True}
    if comment:
        body["comment"] = comment
    await client.post(f"/me/events/{event_id}/tentativelyAccept", json=body)
    return f"Event {event_id} tentatively accepted."


async def outlook__create_online_meeting(
    client: GraphClient,
    subject: str,
    start: str,
    end: str,
) -> str:
    """Create a Teams online meeting link."""
    body = {
        "subject": subject,
        "startDateTime": start,
        "endDateTime": end,
    }
    result = await client.post("/me/onlineMeetings", json=body)
    join_url = result.get("joinWebUrl", "")
    meeting_id = result.get("id", "")
    return f"Online meeting created: '{subject}'\nJoin URL: {join_url}\nMeeting ID: {meeting_id}"


# ── Registry ──────────────────────────────────────────────────────────────────


def register_calendar_tools(registry: ToolRegistry, client: GraphClient) -> int:
    """Register all 12 Calendar tools. Returns count."""

    def _h(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def wrapper(**kwargs: Any) -> str:
            return str(await fn(client, **kwargs))

        return wrapper

    registry.register(
        name="outlook__list_calendars",
        description="List all Microsoft 365 calendars.",
        handler=_h(outlook__list_calendars),
        parameters={"type": "object", "properties": {}, "required": []},
    )
    registry.register(
        name="outlook__list_events",
        description="List calendar events in a date range (default: next 7 days).",
        handler=_h(outlook__list_events),
        parameters={
            "type": "object",
            "properties": {
                "calendar_id": {"type": "string", "default": "", "description": "Calendar ID (default: primary)"},
                "time_min": {"type": "string", "default": "", "description": "Start ISO 8601 (default: now)"},
                "time_max": {"type": "string", "default": "", "description": "End ISO 8601 (default: +7d)"},
                "max_results": {"type": "integer", "default": 50},
            },
            "required": [],
        },
    )
    registry.register(
        name="outlook__get_event",
        description="Get full details of a calendar event by ID.",
        handler=_h(outlook__get_event),
        parameters={
            "type": "object",
            "properties": {"event_id": {"type": "string"}},
            "required": ["event_id"],
        },
    )
    registry.register(
        name="outlook__search_events",
        description="Search calendar events by subject text.",
        handler=_h(outlook__search_events),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
    )
    registry.register(
        name="outlook__check_availability",
        description="Check free/busy availability for one or more people by email.",
        handler=_h(outlook__check_availability),
        parameters={
            "type": "object",
            "properties": {
                "emails": {"type": "string", "description": "Comma-separated email addresses"},
                "time_min": {"type": "string", "default": ""},
                "time_max": {"type": "string", "default": ""},
            },
            "required": ["emails"],
        },
    )
    registry.register(
        name="outlook__create_event",
        description="Create a new Microsoft 365 calendar event with optional attendees, location, and Teams link.",
        handler=_h(outlook__create_event),
        parameters={
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "Event title"},
                "start": {"type": "string", "description": "Start ISO 8601"},
                "end": {"type": "string", "description": "End ISO 8601"},
                "description": {"type": "string", "default": ""},
                "location": {"type": "string", "default": ""},
                "attendees": {"type": "string", "default": "", "description": "Comma-separated emails"},
                "timezone": {"type": "string", "default": "UTC"},
                "is_online_meeting": {"type": "boolean", "default": False, "description": "Add Teams meeting link"},
            },
            "required": ["subject", "start", "end"],
        },
        require_approval=True,
    )
    registry.register(
        name="outlook__update_event",
        description="Update fields on an existing calendar event.",
        handler=_h(outlook__update_event),
        parameters={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "subject": {"type": "string", "default": ""},
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
        name="outlook__delete_event",
        description="Delete (cancel) a calendar event.",
        handler=_h(outlook__delete_event),
        parameters={
            "type": "object",
            "properties": {"event_id": {"type": "string"}},
            "required": ["event_id"],
        },
        require_approval=True,
    )
    registry.register(
        name="outlook__accept_event",
        description="Accept a meeting invitation.",
        handler=_h(outlook__accept_event),
        parameters={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "comment": {"type": "string", "default": ""},
            },
            "required": ["event_id"],
        },
        require_approval=True,
    )
    registry.register(
        name="outlook__decline_event",
        description="Decline a meeting invitation.",
        handler=_h(outlook__decline_event),
        parameters={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "comment": {"type": "string", "default": ""},
            },
            "required": ["event_id"],
        },
        require_approval=True,
    )
    registry.register(
        name="outlook__tentative_event",
        description="Tentatively accept a meeting invitation.",
        handler=_h(outlook__tentative_event),
        parameters={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "comment": {"type": "string", "default": ""},
            },
            "required": ["event_id"],
        },
        require_approval=True,
    )
    return 11
