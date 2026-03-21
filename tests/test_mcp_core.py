"""Tests for MCPServiceCore — agent-independent MCP coordination layer."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pincer.mcp.approval import PolicyApprovalBackend
from pincer.mcp.core import MCPServiceCore, ServerToolDef


def _make_config(enabled=True, servers=None, server_enabled=False):
    """Build a minimal MCPConfig-like mock."""
    cfg = MagicMock()
    cfg.enabled = enabled
    cfg.servers = servers or []

    server_cfg = MagicMock()
    server_cfg.enabled = server_enabled
    server_cfg.host = "127.0.0.1"
    server_cfg.port = 18800
    server_cfg.path = "/mcp"
    server_cfg.expose_tools = ["web_search"]
    server_cfg.sampling = MagicMock(enabled=False)
    cfg.server = server_cfg

    return cfg


def _make_core(servers=None, server_enabled=False) -> MCPServiceCore:
    cfg = _make_config(servers=servers, server_enabled=server_enabled)
    approval = PolicyApprovalBackend({"default": "deny"})
    return MCPServiceCore(config=cfg, approval_backend=approval)


# ── register_tool ──────────────────────────────────────────────────────────────


def test_register_tool():
    core = _make_core()

    async def handler(**kwargs):
        return "result"

    core.register_tool(
        name="pincer_web_search",
        handler=handler,
        schema={"type": "object", "properties": {"query": {"type": "string"}}},
        approval_required=False,
        description="Search the web",
    )
    assert "pincer_web_search" in core._server_tools
    assert core.server_tool_count == 1


def test_register_tool_approval_required():
    core = _make_core()

    async def handler(**kwargs):
        return "ok"

    core.register_tool(
        name="pincer_shell_exec",
        handler=handler,
        schema={"type": "object", "properties": {}},
        approval_required=True,
    )
    assert core._server_tools["pincer_shell_exec"].approval_required is True


# ── register_resource ──────────────────────────────────────────────────────────


def test_register_resource():
    core = _make_core()

    async def handler() -> str:
        return "memory data"

    core.register_resource(
        uri="pincer://memory/recent",
        handler=handler,
        description="Recent memory",
    )
    resources = core.resource_registry.list_all()
    assert len(resources) == 1
    assert resources[0].uri == "pincer://memory/recent"


# ── register_prompt ────────────────────────────────────────────────────────────


def test_register_prompt():
    core = _make_core()

    async def handler(text: str):
        return []

    core.register_prompt(
        name="summarize",
        handler=handler,
        description="Summarize text",
        arguments=[{"name": "text", "required": True, "description": "Text"}],
    )
    prompts = core.prompt_registry.list_all()
    assert len(prompts) == 1
    assert prompts[0].name == "summarize"


# ── start_clients ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_clients_no_servers():
    core = _make_core(servers=[])
    results = await core.start_clients()
    assert results == {}


@pytest.mark.asyncio
async def test_start_clients_disabled():
    cfg = _make_config(enabled=False)
    approval = PolicyApprovalBackend({})
    core = MCPServiceCore(config=cfg, approval_backend=approval)
    results = await core.start_clients()
    assert results == {}


@pytest.mark.asyncio
async def test_start_clients_delegates_to_manager():
    core = _make_core(servers=[MagicMock(enabled=True, name="github")])

    mock_manager = AsyncMock()
    mock_manager.start = AsyncMock(return_value={"github": True})

    with patch("pincer.mcp.core.MCPClientManager", return_value=mock_manager), \
         patch("pincer.mcp.core.MCPSecurityGate"), \
         patch("pincer.mcp.core.MCPAuditLogger"):
        results = await core.start_clients()

    assert results == {"github": True}
    mock_manager.start.assert_called_once()


# ── stop_clients ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_clients_no_manager():
    core = _make_core()
    # Should not raise even if no manager was started
    await core.stop_clients()
    assert core._client_manager is None


@pytest.mark.asyncio
async def test_stop_clients_calls_manager_stop():
    core = _make_core()
    mock_manager = AsyncMock()
    core._client_manager = mock_manager

    await core.stop_clients()
    mock_manager.stop.assert_called_once()
    assert core._client_manager is None


# ── start_server ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_server_not_enabled():
    core = _make_core(server_enabled=False)
    # Should be a no-op
    await core.start_server()
    assert core._server is None


@pytest.mark.asyncio
async def test_start_server_enabled():
    core = _make_core(server_enabled=True)

    async def handler(**kwargs):
        return "ok"

    core.register_tool(
        name="pincer_web_search",
        handler=handler,
        schema={"type": "object", "properties": {}},
        approval_required=False,
    )

    mock_server = AsyncMock()
    mock_server.running = True
    mock_server.endpoint = "http://127.0.0.1:18800/mcp"

    with patch("pincer.mcp.core.PincerMCPServer", return_value=mock_server):
        await core.start_server()

    mock_server.start.assert_called_once()
    assert core.server_running


# ── stop_server ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_server_no_server():
    core = _make_core()
    await core.stop_server()  # no-op


@pytest.mark.asyncio
async def test_stop_server_calls_server_stop():
    core = _make_core()
    mock_server = AsyncMock()
    core._server = mock_server

    await core.stop_server()
    mock_server.stop.assert_called_once()
    assert core._server is None


# ── properties ─────────────────────────────────────────────────────────────────


def test_server_running_false_initially():
    core = _make_core()
    assert not core.server_running


def test_server_endpoint_none_initially():
    core = _make_core()
    assert core.server_endpoint is None


def test_client_tool_count_zero_initially():
    core = _make_core()
    assert core.client_tool_count == 0


@pytest.mark.asyncio
async def test_list_connected_clients_empty():
    core = _make_core()
    result = await core.list_connected_clients()
    assert result == []
