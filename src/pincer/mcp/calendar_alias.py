"""
``pincer_calendar_today`` — kept for external MCP clients after the builtin
calendar tools were removed from the model's registry.

An MCP-only alias (never registered in the ToolRegistry, so the model never
sees it): today's events through the Workspace list, where "today" is the
calendar's own day. Near midnight a Kyiv calendar on a Berlin deployment is
already on the next day; using the clock's day would list the wrong one.

The output is the list tool's own (with event IDs — MCP clients are agents).
Without a Google factory it falls back to the registry's list tool for the
clock zone's day, and to a plain "not connected" when that is missing too.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pincer.integrations.google.calendar_source import CalendarSource

if TYPE_CHECKING:
    from pincer.tools.registry import ToolRegistry

CALENDAR_TODAY = "calendar_today"
LIST_TOOL = "google__list_events"
DESCRIPTION = "List today's Google Calendar events (today and times in the calendar's own timezone)."
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"calendar_id": {"type": "string", "default": "primary"}},
    "required": [],
}
NOT_CONNECTED = "Google Calendar is not connected."


def _now() -> datetime:
    return datetime.now(UTC)


class CalendarTodayAlias:
    """The ``pincer_calendar_today`` handler for one MCP server."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry
        self._source = CalendarSource("MCP pincer_calendar_today")

    async def __call__(self, calendar_id: str = "primary") -> str:
        from pincer.integrations.google.tools_calendar import calendar_today, google__list_events

        now = _now()
        factory = self._source.factory()
        if factory is not None:
            day = (await calendar_today(factory, calendar_id, now=now)).isoformat()
            return await google__list_events(factory, calendar_id, time_min=day, time_max=day)
        if LIST_TOOL not in self._registry.list_tools():
            return NOT_CONNECTED
        from pincer.integrations.google.calendar_tz import clock_zone

        day = now.astimezone(clock_zone("")).date().isoformat()
        return await self._registry.execute(LIST_TOOL, {"calendar_id": calendar_id, "time_min": day, "time_max": day})
