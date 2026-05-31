"""
Tests for the standalone Microsoft 365 MCP server.

All tests reuse the mocked GraphClient fixtures from conftest.py — no real
Microsoft Graph calls are made.
"""

from __future__ import annotations

import pytest

from pincer.integrations.ms365 import mcp_server

# Expected tool count per service (matches the in-agent integration).
_EXPECTED_COUNTS = {
    "email": 17,
    "calendar": 12,
    "onedrive": 14,
    "todo": 8,
    "teams": 7,
    "contacts": 6,
    "onenote": 5,
}
_TOTAL = sum(_EXPECTED_COUNTS.values())  # 69


def test_collect_all_tools_count(mock_client):
    specs = mcp_server.collect_tools(mock_client)
    assert len(specs) == _TOTAL


def test_collect_per_service_counts(mock_client):
    for service, expected in _EXPECTED_COUNTS.items():
        specs = mcp_server.collect_tools(mock_client, services=[service])
        assert len(specs) == expected, f"{service} expected {expected}, got {len(specs)}"


def test_unique_names_and_nonempty_schemas(mock_client):
    specs = mcp_server.collect_tools(mock_client)
    names = [s.name for s in specs]
    assert len(names) == len(set(names)), "tool names must be unique"
    for s in specs:
        assert s.description, f"{s.name} missing description"
        assert s.parameters.get("type") == "object", f"{s.name} schema not an object"


def test_read_only_drops_write_tools(mock_client):
    full = mcp_server.collect_tools(mock_client)
    read_only = mcp_server.collect_tools(mock_client, read_only=True)
    assert len(read_only) < len(full)
    assert all(not s.require_approval for s in read_only)
    # A known write tool must be excluded, a known read tool included.
    ro_names = {s.name for s in read_only}
    assert "outlook__send_message" not in ro_names
    assert "outlook__list_messages" in ro_names


def test_unknown_service_skipped(mock_client):
    specs = mcp_server.collect_tools(mock_client, services=["email", "does_not_exist"])
    assert len(specs) == _EXPECTED_COUNTS["email"]


@pytest.mark.asyncio
async def test_build_server_lists_all_tools(mock_client):
    server, specs = mcp_server.build_server(mock_client)
    assert len(specs) == _TOTAL
    handler = server.request_handlers
    # The low-level Server registers a ListToolsRequest handler.
    import mcp.types as types

    list_handler = handler[types.ListToolsRequest]
    result = await list_handler(types.ListToolsRequest(method="tools/list"))
    tools = result.root.tools
    assert len(tools) == _TOTAL
    # Annotations reflect approval flags.
    by_name = {t.name: t for t in tools}
    assert by_name["outlook__send_message"].annotations.destructiveHint is True
    assert by_name["outlook__list_messages"].annotations.readOnlyHint is True


@pytest.mark.asyncio
async def test_call_tool_dispatches_to_handler(mock_client):
    server, _ = mcp_server.build_server(mock_client)
    import mcp.types as types

    call_handler = server.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(
            name="outlook__list_messages",
            arguments={"folder": "Inbox", "max_results": 5},
        ),
    )
    result = await call_handler(req)
    content = result.root.content
    assert content and content[0].type == "text"
    mock_client.get.assert_awaited()


@pytest.mark.asyncio
async def test_call_unknown_tool_returns_error(mock_client):
    server, _ = mcp_server.build_server(mock_client)
    import mcp.types as types

    call_handler = server.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name="nope__nope", arguments={}),
    )
    result = await call_handler(req)
    assert "Unknown tool" in result.root.content[0].text


def test_build_client_exits_without_client_id(monkeypatch):
    import pincer.integrations.ms365.config as cfg_mod

    def fake_load_config():
        c = cfg_mod.MS365IntegrationConfig()
        c.client_id = ""
        return c

    monkeypatch.setattr(mcp_server, "load_config", fake_load_config, raising=False)
    monkeypatch.setattr("pincer.integrations.ms365.config.load_config", fake_load_config)
    with pytest.raises(SystemExit):
        mcp_server._build_client_or_exit(None)


def test_build_client_exits_without_token(monkeypatch):
    import pincer.integrations.ms365.config as cfg_mod

    def fake_load_config():
        c = cfg_mod.MS365IntegrationConfig()
        c.client_id = "test-client-id"
        return c

    class _NoTokenAuth:
        def __init__(self, *args, **kwargs):
            pass

        def has_cached_token(self):
            return False

    monkeypatch.setattr("pincer.integrations.ms365.config.load_config", fake_load_config)
    monkeypatch.setattr("pincer.integrations.ms365.auth.MS365Auth", _NoTokenAuth)
    with pytest.raises(SystemExit):
        mcp_server._build_client_or_exit(None)
