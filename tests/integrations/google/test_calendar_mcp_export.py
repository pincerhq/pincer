"""
Pincer's MCP server after the builtin calendar module is gone (slice E).

External clients keep ``pincer_calendar_today``: an MCP-only alias that lists
today's events through the Workspace list tool (it is never put back into the
model's registry). Default exposed tools and the scope map name the Workspace
calendar tools, and an exported name that is not registered logs a warning.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import logging
import pathlib
import tomllib
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from integrations.google.fake_calendar import FakeCalendarFactory, FakeCalendarService
from pincer.integrations.google.calendar_render import EDIT_HINT
from pincer.integrations.google.tools_calendar import register_calendar_tools
from pincer.mcp.auth.scopes import check_tool_scope
from pincer.mcp.calendar_alias import NOT_CONNECTED
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
    """A FastMCP stand-in that keeps each registered handler so a test can call it directly.

    Direct calls, not FastMCP's call_tool: the exporter registers registry tools as
    ``_handler(**kwargs)``, which FastMCP turns into one required "kwargs" argument, so
    real call_tool cannot reach them (pre-existing; an out-of-scope follow-up in the PRD).
    """
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
    # Calls the exporter's handler directly (see _fmcp for why not FastMCP's call_tool).
    text = await fmcp.tools["pincer_calendar_today"]()
    assert "Lunch with Ana" in text
    assert "Tomorrow's call" not in text
    assert "calendar_today" not in registry.list_tools()  # MCP-only, never offered to the model


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

    # Calls the exporter's handler directly (see _fmcp for why not FastMCP's call_tool).
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
        ("google__move_event", "tools:write"),
        ("google__accept_event", "tools:write"),
        ("google__decline_event", "tools:write"),
        ("google__add_google_meet", "tools:write"),
        ("google__move_event", "tools:execute"),
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


# ── What external clients are actually served (review #1, #8, #9) ─────────────


@pytest.fixture
def no_network(monkeypatch):
    """FastMCP registers tools for real; only its HTTP serving loop is replaced."""

    async def _serve_forever(_self) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr("mcp.server.fastmcp.FastMCP.run_streamable_http_async", _serve_forever)


def _served_text(result) -> str:
    blocks = result[0] if isinstance(result, tuple) else result
    return "\n".join(getattr(b, "text", "") for b in blocks)


async def _served(fmcp) -> tuple[set[str], str]:
    names = {t.name for t in await fmcp.list_tools()}
    text = _served_text(await fmcp.call_tool("pincer_calendar_today", {})) if "pincer_calendar_today" in names else ""
    return names, text


async def test_mcp_serve_serves_pincer_calendar_today(fake, google_token, no_network, monkeypatch) -> None:
    """`pincer mcp serve` (StandaloneMCPShell → MCPServiceCore) with the server enabled."""
    from pincer.mcp.config import MCPConfig
    from pincer.mcp.standalone import StandaloneMCPShell

    monkeypatch.setattr("pincer.mcp.standalone._load_builtin_tools_into", lambda _registry: None)
    _seed_today(fake)
    shell = StandaloneMCPShell(mcp_config=MCPConfig(server=MCPServerExportConfig(enabled=True)))
    task = asyncio.create_task(shell.run())
    try:
        for _ in range(200):
            if shell.core is not None and shell.core._server is not None and shell.core._server._fmcp is not None:
                break
            await asyncio.sleep(0.01)
        names, text = await _served(shell.core._server._fmcp)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert "pincer_calendar_today" in names
    assert "Lunch with Ana" in text
    assert "Tomorrow's call" not in text
    assert EDIT_HINT not in text


async def test_embedded_server_enabled_serves_pincer_calendar_today(registry, fake, no_network) -> None:
    from pincer.mcp.config import MCPConfig

    _seed_today(fake)
    shell = EmbeddedMCPShell(agent=_agent(registry), mcp_config=MCPConfig(server=MCPServerExportConfig(enabled=True)))
    await shell.start()
    try:
        names, text = await _served(shell.core._server._fmcp)
    finally:
        await shell.stop()
    assert "pincer_calendar_today" in names
    assert "Lunch with Ana" in text
    assert EDIT_HINT not in text


async def test_pincer_run_export_strips_the_edit_hint(registry, fake) -> None:
    """`pincer run` exports the real registry: clients get no hint naming a tool they lack
    (google__update_event is not exported, and would be pincer_google__update_event)."""
    _seed_today(fake)
    llm_text = await registry.execute("google__search_events", {"query": "Lunch"})
    assert llm_text.splitlines()[-1] == EDIT_HINT  # the model still gets it
    fmcp = _fmcp()
    expose = ["calendar_today", "google__list_events", "google__search_events", "google__get_event"]
    PincerToolExporter(registry, expose_tools=expose).export_to(fmcp)
    # Calls the exporter's handler directly (see _fmcp for why not FastMCP's call_tool).
    texts = {
        "list": await fmcp.tools["pincer_google__list_events"](time_min=datetime.now(KYIV).date().isoformat()),
        "search": await fmcp.tools["pincer_google__search_events"](query="Lunch"),
        "get": await fmcp.tools["pincer_google__get_event"](event_id="evt1"),
        "alias": await fmcp.tools["pincer_calendar_today"](),
    }
    for name, text in texts.items():
        assert "Lunch with Ana" in text, (name, text)
        assert EDIT_HINT not in text and "google__update_event" not in text, (name, text)


async def test_alias_without_a_factory_is_not_connected(registry, fake, monkeypatch) -> None:
    """A failed Google setup is cached for the process: never fall back to the clock's day."""
    monkeypatch.setattr("pincer.integrations.google.calendar_source._resolve", lambda: (None, "setup failed"))
    _seed_today(fake)
    fmcp = _fmcp()
    PincerToolExporter(registry, expose_tools=["calendar_today"]).export_to(fmcp)

    assert "google__list_events" in registry.list_tools()
    # Calls the exporter's handler directly (see _fmcp for why not FastMCP's call_tool).
    assert await fmcp.tools["pincer_calendar_today"]() == NOT_CONNECTED


def test_shipped_pincer_toml_exports_the_workspace_calendar_reads() -> None:
    """pincer.toml (copied into the Docker image) overrides the defaults: it must carry the reads too."""
    shipped = (SRC.parent / "pincer.toml").read_text(encoding="utf-8")
    exposed = tomllib.loads(shipped)["mcp"]["server"]["expose_tools"]
    assert {"calendar_today", "google__list_events", "google__search_events"} <= set(exposed)
    assert not {"calendar_week", "calendar_create"} & set(exposed)
    assert "calendar_create" not in shipped  # not even as a commented-out suggestion


def test_no_config_names_the_deleted_builtin_module() -> None:
    assert "builtin.calendar_tool" not in (SRC.parent / "pyproject.toml").read_text(encoding="utf-8")
