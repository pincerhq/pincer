"""
``pincer_calendar_today`` — kept for external MCP clients after the builtin
calendar tools were removed from the model's registry.

An MCP-only alias (never registered in the ToolRegistry, so the model never
sees it), served by ``PincerToolExporter`` on every MCP server (``pincer run``
and ``pincer mcp serve``): today's events through the Workspace list, where
"today" is the calendar's own day. Near midnight a Kyiv calendar on a Berlin
deployment is already on the next day; using the clock's day would list the
wrong one.

The output is the list tool's own (with event IDs — MCP clients are agents),
minus the model's edit hint, which names a tool clients do not have. Without a
Google factory it says "not connected": there is no calendar day to list without
the calendar.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import MappingProxyType

from pincer.integrations.google.calendar_render import EDIT_HINT_KEY
from pincer.integrations.google.calendar_source import CalendarSource
from pincer.integrations.google.tools_calendar import calendar_today, google__list_events

CALENDAR_TODAY = "calendar_today"
DESCRIPTION = "List today's Google Calendar events (today and times in the calendar's own timezone)."
NOT_CONNECTED = "Google Calendar is not connected."
#: The tool context of every MCP call: no edit hint (it names a tool clients lack).
#: Read-only (shared by every call); each use site passes its own copy.
MCP_TOOL_CONTEXT = MappingProxyType({EDIT_HINT_KEY: False})


def _now() -> datetime:
    return datetime.now(UTC)


class CalendarTodayAlias:
    """The ``pincer_calendar_today`` handler for one MCP server."""

    def __init__(self) -> None:
        self._source = CalendarSource("MCP pincer_calendar_today")

    async def __call__(self, calendar_id: str = "primary") -> str:
        factory = self._source.factory()
        if factory is None:
            return NOT_CONNECTED
        day = (await calendar_today(factory, calendar_id, now=_now())).isoformat()
        return await google__list_events(
            factory, calendar_id, time_min=day, time_max=day, context=dict(MCP_TOOL_CONTEXT)
        )
