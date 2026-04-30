"""Tests for EmbeddedMCPShell."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pincer.mcp.embedded import EmbeddedMCPShell


def _make_agent(expose_tools=None):
    """Build a minimal agent-like mock."""
    agent = MagicMock()
    agent._ask_user_callback = None
    agent._ask_user_on_active_channel = AsyncMock(return_value="user reply")
    agent._audit_log = None

    registry = MagicMock()
    registry.list_tools = MagicMock(return_value=expose_tools or ["web_search"])
    registry.requires_approval = MagicMock(return_value=False)

    tool_def = MagicMock()
    tool_def.handler = AsyncMock(return_value="search result")
    tool_def.parameters = {"type": "object", "properties": {"query": {"type": "string"}}}
    tool_def.description = "Search the web"

    registry._tools = {t: tool_def for t in (expose_tools or ["web_search"])}
    agent.tool_registry = registry

    return agent


def _make_mcp_config(server_enabled=False, expose_tools=None):
    cfg = MagicMock()
    cfg.enabled = True
    cfg.servers = []

    server_cfg = MagicMock()
    server_cfg.enabled = server_enabled
    server_cfg.expose_tools = expose_tools or ["web_search"]
    server_cfg.sampling = MagicMock(enabled=False)
    cfg.server = server_cfg

    return cfg


# ── Lifecycle ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_embedded_start_no_servers():
    agent = _make_agent()
    cfg = _make_mcp_config()

    shell = EmbeddedMCPShell(agent=agent, mcp_config=cfg)
    results = await shell.start()

    assert results == {}
    assert hasattr(agent, "mcp_shell")
    assert agent.mcp_shell is shell

    await shell.stop()


@pytest.mark.asyncio
async def test_embedded_start_registers_tools_for_export():
    agent = _make_agent(expose_tools=["web_search"])
    cfg = _make_mcp_config(expose_tools=["web_search"])

    shell = EmbeddedMCPShell(agent=agent, mcp_config=cfg)
    await shell.start()

    core = shell.core
    assert core is not None
    # pincer_web_search should be registered in core's server tools
    assert "pincer_web_search" in core._server_tools

    await shell.stop()


@pytest.mark.asyncio
async def test_embedded_start_registers_ask_user_tool():
    agent = _make_agent()
    cfg = _make_mcp_config()

    shell = EmbeddedMCPShell(agent=agent, mcp_config=cfg)
    await shell.start()

    core = shell.core
    assert "pincer_ask_user" in core._server_tools

    await shell.stop()


@pytest.mark.asyncio
async def test_embedded_start_registers_resources():
    agent = _make_agent()
    cfg = _make_mcp_config()

    shell = EmbeddedMCPShell(agent=agent, mcp_config=cfg)
    await shell.start()

    core = shell.core
    resource_uris = [r.uri for r in core.resource_registry.list_all()]
    assert "pincer://memory/recent" in resource_uris
    assert "pincer://agent/status" in resource_uris

    await shell.stop()


@pytest.mark.asyncio
async def test_embedded_start_connects_mcp_clients():
    agent = _make_agent()
    cfg = _make_mcp_config()
    cfg.servers = [MagicMock(enabled=True, name="github")]

    shell = EmbeddedMCPShell(agent=agent, mcp_config=cfg)

    mock_manager = AsyncMock()
    mock_manager.start = AsyncMock(return_value={"github": True})
    mock_manager.stop = AsyncMock()
    mock_manager.total_tool_count = MagicMock(return_value=5)

    with (
        patch("pincer.mcp.core.MCPClientManager", return_value=mock_manager),
        patch("pincer.mcp.core.MCPSecurityGate"),
        patch("pincer.mcp.core.MCPAuditLogger"),
    ):
        results = await shell.start()

    assert results == {"github": True}
    await shell.stop()


@pytest.mark.asyncio
async def test_embedded_start_with_server():
    agent = _make_agent()
    cfg = _make_mcp_config(server_enabled=True)

    shell = EmbeddedMCPShell(agent=agent, mcp_config=cfg)

    mock_server = AsyncMock()
    mock_server.running = True
    mock_server.endpoint = "http://127.0.0.1:18800/mcp"

    with patch("pincer.mcp.core.PincerMCPServer", return_value=mock_server):
        await shell.start()

    mock_server.start.assert_called_once()
    assert shell.server_running

    with patch("pincer.mcp.core.PincerMCPServer", return_value=mock_server):
        await shell.stop()


@pytest.mark.asyncio
async def test_embedded_stop_idempotent():
    agent = _make_agent()
    cfg = _make_mcp_config()

    shell = EmbeddedMCPShell(agent=agent, mcp_config=cfg)
    await shell.start()
    await shell.stop()
    await shell.stop()  # second stop should not raise


@pytest.mark.asyncio
async def test_embedded_ask_user_routes_to_agent():
    agent = _make_agent()
    agent._ask_user_on_active_channel = AsyncMock(return_value="agent answer")
    cfg = _make_mcp_config()

    shell = EmbeddedMCPShell(agent=agent, mcp_config=cfg)
    await shell.start()

    core = shell.core
    ask_tool = core._server_tools.get("pincer_ask_user")
    assert ask_tool is not None

    result = await ask_tool.handler(question="What is your name?")
    assert result == "agent answer"
    agent._ask_user_on_active_channel.assert_called_once_with("What is your name?")

    await shell.stop()


@pytest.mark.asyncio
async def test_embedded_skips_missing_tools():
    agent = _make_agent(expose_tools=["web_search"])
    cfg = _make_mcp_config(expose_tools=["web_search", "nonexistent_tool"])

    # nonexistent_tool is NOT in registry
    agent.tool_registry.list_tools = MagicMock(return_value=["web_search"])

    shell = EmbeddedMCPShell(agent=agent, mcp_config=cfg)
    await shell.start()

    core = shell.core
    # Only web_search should be registered
    assert "pincer_web_search" in core._server_tools
    assert "pincer_nonexistent_tool" not in core._server_tools

    await shell.stop()
