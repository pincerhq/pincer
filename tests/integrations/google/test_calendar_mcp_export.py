"""
Pincer's MCP server after the builtin calendar module is gone (slice E).

External clients keep ``pincer_calendar_today``: an MCP-only alias that lists
today's events through the Workspace list tool (it is never put back into the
model's registry). Default exposed tools and the scope map name the Workspace
calendar tools, and an exported name that is not registered logs a warning.
"""

from __future__ import annotations

import importlib.util
import logging
import pathlib
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from integrations.google.fake_calendar import FakeCalendarFactory, FakeCalendarService
from pincer.integrations.google.tools_calendar import register_calendar_tools
from pincer.mcp.auth.scopes import check_tool_scope
from pincer.mcp.config import MCPServerExportConfig
from pincer.mcp.embedded import EmbeddedMCPShell
from pincer.mcp.exporter import PincerToolExporter
from pincer.tools.registry import ToolRegistry

KYIV = ZoneInfo("Europe/Kyiv")
BERLIN = ZoneInfo("Europe/Berlin")
SRC = pathlib.Path(__file__).resolve().parents[3] / "src"


@pytest.fixture
def fake() -> FakeCalendarService:
    return FakeCalendarService({"primary": "Europe/Kyiv"})


@pytest.fixture
def registry(fake, google_token) -> ToolRegistry:
    reg = ToolRegistry()
    register_calendar_tools(reg, FakeCalendarFactory(fake))
    return reg


def _seed_today(fake: FakeCalendarService) -> None:
    """One event at noon today in the calendar's zone (Kyiv) and one tomorrow."""
    today = datetime.now(KYIV).date()
    noon = datetime.combine(today, datetime.min.time(), KYIV).replace(hour=12)
    fake.seed_event("primary", "Lunch with Ana", noon.isoformat(), (noon + timedelta(hours=1)).isoformat())
    tomorrow = noon + timedelta(days=1)
    fake.seed_event("primary", "Tomorrow's call", tomorrow.isoformat(), (tomorrow + timedelta(hours=1)).isoformat())


def _fmcp() -> MagicMock:
    fmcp = MagicMock()
    fmcp.tools = {}

    def _add_tool(fn, name=None, **_kwargs):
        fmcp.tools[name or fn.__name__] = fn

    fmcp.add_tool.side_effect = _add_tool
    return fmcp


def _agent(registry: ToolRegistry) -> MagicMock:
    agent = MagicMock()
    agent._ask_user_callback = None
    agent._ask_user_on_active_channel = AsyncMock(return_value="ok")
    agent._audit_log = None
    agent.tool_registry = registry
    return agent


async def test_exporter_keeps_pincer_calendar_today_by_default(registry, fake) -> None:
    _seed_today(fake)
    fmcp = _fmcp()
    PincerToolExporter(registry, expose_tools=MCPServerExportConfig().expose_tools).export_to(fmcp)

    assert "pincer_calendar_today" in fmcp.tools
    text = await fmcp.tools["pincer_calendar_today"]()
    assert "Lunch with Ana" in text
    assert "Tomorrow's call" not in text
    assert "calendar_today" not in registry.list_tools()  # MCP-only, never offered to the model


async def test_embedded_server_keeps_pincer_calendar_today(registry, fake) -> None:
    _seed_today(fake)
    agent = _agent(registry)
    server = SimpleNamespace(
        enabled=False, expose_tools=MCPServerExportConfig().expose_tools, sampling=SimpleNamespace(enabled=False)
    )
    shell = EmbeddedMCPShell(agent=agent, mcp_config=SimpleNamespace(enabled=True, servers=[], server=server))
    await shell.start()
    try:
        tool = shell.core._server_tools.get("pincer_calendar_today")
        assert tool is not None
        text = await tool.handler()
    finally:
        await shell.stop()
    assert "Lunch with Ana" in text
    assert "Tomorrow's call" not in text


async def test_alias_day_is_the_calendar_zone_day_not_the_clock_day(registry, fake, monkeypatch) -> None:
    """23:30 Berlin (the clock) is already 00:30 the next day in Kyiv (the calendar)."""
    late_berlin = datetime(2026, 11, 12, 23, 30, tzinfo=BERLIN)
    monkeypatch.setattr("pincer.mcp.calendar_alias._now", lambda: late_berlin)
    kyiv_13th = datetime(2026, 11, 13, 9, 0, tzinfo=KYIV)
    fake.seed_event("primary", "Kyiv 13th", kyiv_13th.isoformat(), (kyiv_13th + timedelta(hours=1)).isoformat())
    kyiv_12th = datetime(2026, 11, 12, 18, 0, tzinfo=KYIV)
    fake.seed_event("primary", "Kyiv 12th", kyiv_12th.isoformat(), (kyiv_12th + timedelta(hours=1)).isoformat())
    fmcp = _fmcp()
    PincerToolExporter(registry, expose_tools=["calendar_today"]).export_to(fmcp)

    text = await fmcp.tools["pincer_calendar_today"]()

    assert "Kyiv 13th" in text
    assert "Kyiv 12th" not in text


async def test_embedded_export_of_an_unregistered_tool_logs_a_warning(registry, caplog) -> None:
    agent = _agent(registry)
    server = SimpleNamespace(enabled=False, expose_tools=["no_such_tool"], sampling=SimpleNamespace(enabled=False))
    shell = EmbeddedMCPShell(agent=agent, mcp_config=SimpleNamespace(enabled=True, servers=[], server=server))
    with caplog.at_level(logging.DEBUG, logger="pincer.mcp.embedded"):
        await shell.start()
    await shell.stop()
    hits = [r for r in caplog.records if "no_such_tool" in r.getMessage()]
    assert [r.levelno for r in hits] == [logging.WARNING]


def test_default_exports_name_workspace_calendar_tools() -> None:
    exposed = set(MCPServerExportConfig().expose_tools)
    assert "calendar_today" in exposed
    assert {"google__list_events", "google__search_events"} <= exposed
    assert not {"calendar_week", "calendar_create"} & exposed


@pytest.mark.parametrize(
    ("tool", "scope"),
    [
        ("calendar_today", "tools:read"),
        ("google__list_events", "tools:read"),
        ("google__search_events", "tools:read"),
        ("google__get_event", "tools:read"),
        ("google__create_event", "tools:write"),
        ("google__update_event", "tools:write"),
        ("google__list_events", "tools:execute"),
        ("google__update_event", "tools:execute"),
    ],
)
def test_scopes_grant_workspace_calendar_tools(tool, scope) -> None:
    assert check_tool_scope(tool, scope)


def test_scopes_do_not_grant_delete_or_builtin_names() -> None:
    assert not check_tool_scope("google__delete_event", "tools:write")
    assert not check_tool_scope("google__create_event", "tools:read")
    assert not check_tool_scope("calendar_create", "tools:write")


def test_builtin_calendar_module_is_gone_and_unimported() -> None:
    assert importlib.util.find_spec("pincer.tools.builtin.calendar_tool") is None
    importers = [
        str(p.relative_to(SRC))
        for p in SRC.rglob("*.py")
        if "builtin.calendar_tool" in p.read_text(encoding="utf-8", errors="replace")
    ]
    assert importers == []
