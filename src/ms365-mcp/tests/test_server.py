"""Tests for the standalone MS365 MCP server."""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, Any

import pytest
from fastmcp.exceptions import NotFoundError, ToolError
from ms365 import ms365_server

if TYPE_CHECKING:
    from unittest.mock import MagicMock

_EXPECTED_COUNTS = {
    "email": 17,
    "calendar": 11,
    "onedrive": 14,
    "todo": 8,
    "contacts": 6,
    "onenote": 5,
    "directory": 1,
}
_TOTAL = sum(_EXPECTED_COUNTS.values())  # 62


# ── collect_tools ─────────────────────────────────────────────────────────────


def test_collect_all_tools_count(mock_client: MagicMock) -> None:
    specs = ms365_server.collect_tools(mock_client)
    assert len(specs) == _TOTAL


def test_collect_per_service_counts(mock_client: MagicMock) -> None:
    for service, expected in _EXPECTED_COUNTS.items():
        specs = ms365_server.collect_tools(mock_client, services=[service])
        assert len(specs) == expected, f"{service}: expected {expected}, got {len(specs)}"


def test_unique_names_and_nonempty_schemas(mock_client: MagicMock) -> None:
    specs = ms365_server.collect_tools(mock_client)
    names = [s.name for s in specs]
    assert len(names) == len(set(names)), "tool names must be unique"
    for s in specs:
        assert s.description, f"{s.name} missing description"
        assert s.parameters.get("type") == "object", f"{s.name} schema not an object"


def test_read_only_drops_write_tools(mock_client: MagicMock) -> None:
    full = ms365_server.collect_tools(mock_client)
    read_only = ms365_server.collect_tools(mock_client, read_only=True)
    assert len(read_only) < len(full)
    assert all(not s.require_approval for s in read_only)
    ro_names = {s.name for s in read_only}
    assert "outlook__send_message" not in ro_names
    assert "outlook__list_messages" in ro_names


def test_unknown_service_skipped(mock_client: MagicMock) -> None:
    specs = ms365_server.collect_tools(mock_client, services=["email", "does_not_exist"])
    assert len(specs) == _EXPECTED_COUNTS["email"]


def test_write_tools_have_approval_flag(mock_client: MagicMock) -> None:
    specs = ms365_server.collect_tools(mock_client)
    by_name = {s.name: s for s in specs}
    write_tools = [
        "outlook__send_message",
        "outlook__reply_to_message",
        "outlook__reply_all_to_message",
        "outlook__forward_message",
        "outlook__create_draft",
        "outlook__update_draft",
        "outlook__send_draft",
        "outlook__move_message",
        "outlook__delete_message",
        "outlook__create_event",
        "outlook__update_event",
        "outlook__delete_event",
        "outlook__accept_event",
        "outlook__decline_event",
        "outlook__tentative_event",
        "onedrive__upload_file",
        "onedrive__create_folder",
        "onedrive__move_file",
        "onedrive__rename_file",
        "onedrive__copy_file",
        "onedrive__delete_file",
        "onedrive__share_file",
        "ms_todo__create_task",
        "ms_todo__update_task",
        "ms_todo__complete_task",
        "ms_todo__delete_task",
        "ms_todo__create_task_list",
        "outlook__create_contact",
        "outlook__update_contact",
        "outlook__delete_contact",
        "onenote__create_page",
    ]
    for name in write_tools:
        assert by_name[name].require_approval, f"{name} should require approval"


def test_read_tools_no_approval_flag(mock_client: MagicMock) -> None:
    specs = ms365_server.collect_tools(mock_client)
    by_name = {s.name: s for s in specs}
    read_tools = [
        "outlook__list_mail_folders",
        "outlook__list_messages",
        "outlook__search_messages",
        "outlook__get_message",
        "outlook__get_message_attachments",
        "outlook__download_attachment",
        "outlook__list_calendars",
        "outlook__list_events",
        "outlook__get_event",
        "outlook__search_events",
        "outlook__check_availability",
        "onedrive__list_drive_items",
        "onedrive__search_files",
        "onedrive__get_file_metadata",
        "onedrive__download_file",
        "onedrive__get_file_preview",
        "onedrive__list_shared_with_me",
        "onedrive__list_recent_files",
        "ms_todo__list_task_lists",
        "ms_todo__list_tasks",
        "ms_todo__get_task",
        "outlook__list_contacts",
        "outlook__search_contacts",
        "outlook__get_contact",
        "onenote__list_notebooks",
        "onenote__list_sections",
        "onenote__list_pages",
        "onenote__get_page_content",
        "ms365__search_users",
    ]
    for name in read_tools:
        assert not by_name[name].require_approval, f"{name} should not require approval"


def test_low_risk_write_tools_no_approval(mock_client: MagicMock) -> None:
    specs = ms365_server.collect_tools(mock_client)
    by_name = {s.name: s for s in specs}
    assert not by_name["outlook__mark_as_read"].require_approval
    assert not by_name["outlook__flag_message"].require_approval


# ── build_server ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_server_lists_all_tools(mock_client: MagicMock) -> None:
    mcp, specs = ms365_server.build_server(mock_client)
    assert len(specs) == _TOTAL

    tools = await mcp.list_tools()
    assert len(tools) == _TOTAL

    by_name = {t.name: t for t in tools}
    send_message_annotations = by_name["outlook__send_message"].annotations
    list_messages_annotations = by_name["outlook__list_messages"].annotations
    assert send_message_annotations is not None
    assert list_messages_annotations is not None
    assert send_message_annotations.destructiveHint is True
    assert list_messages_annotations.readOnlyHint is True


@pytest.mark.asyncio
async def test_build_server_schema_matches_spec_parameters(mock_client: MagicMock) -> None:
    """FastMCP derives each tool's JSON schema from the handler's real signature —
    confirm it lines up with the `parameters` dict `collect_tools()` still carries
    (and, crucially, doesn't leak the bound `client: GraphClient` argument)."""
    mcp, _ = ms365_server.build_server(mock_client)
    tool = await mcp.get_tool("outlook__list_messages")
    assert tool is not None
    schema = tool.parameters
    assert "client" not in schema["properties"]
    assert schema["properties"]["folder"]["default"] == "Inbox"
    assert schema["properties"]["max_results"]["default"] == 25


@pytest.mark.asyncio
async def test_call_tool_dispatches_to_handler(mock_client: MagicMock) -> None:
    mcp, _ = ms365_server.build_server(mock_client)
    result = await mcp.call_tool("outlook__list_messages", {"folder": "Inbox", "max_results": 5})
    content = result.content
    assert content and content[0].type == "text"
    mock_client.get.assert_awaited()


@pytest.mark.asyncio
async def test_call_unknown_tool_raises(mock_client: MagicMock) -> None:
    """FastMCP raises for an unregistered tool name instead of returning
    "[Error] Unknown tool: ..." as ordinary text — a deliberate behavior change
    from the low-level SDK version. A real MCP client sees this as a protocol
    error result (`isError=True`) rather than a disguised successful response;
    Pincer's own bridge (`pincer/mcp/bridge.py::_format_result`) already extracts
    the text content regardless of `isError`, so the LLM still sees a clear
    error string either way."""
    mcp, _ = ms365_server.build_server(mock_client)
    with pytest.raises(NotFoundError, match="nope__nope"):
        await mcp.call_tool("nope__nope", {})


# ── build_http_app ─────────────────────────────────────────────────────────────


def test_build_http_app_health_endpoint(mock_client: MagicMock) -> None:
    from starlette.testclient import TestClient

    mcp, _ = ms365_server.build_server(mock_client)
    app = ms365_server.build_http_app(mcp)

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["service"] == "ms365-mcp"


def test_build_http_app_root_endpoint(mock_client: MagicMock) -> None:
    from starlette.testclient import TestClient

    mcp, _ = ms365_server.build_server(mock_client)
    app = ms365_server.build_http_app(mcp)

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_build_http_app_has_mcp_route(mock_client: MagicMock) -> None:
    mcp, _ = ms365_server.build_server(mock_client)
    app = ms365_server.build_http_app(mcp)
    route_paths = [r.path for r in app.routes]
    assert "/mcp" in route_paths


# ── _build_client ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_client_exits_without_client_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MS365_CLIENT_ID", raising=False)
    with pytest.raises(SystemExit):
        await ms365_server._build_client(None)


@pytest.mark.asyncio
async def test_build_client_runs_device_flow_when_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    monkeypatch.setenv("MS365_CLIENT_ID", "test-client-id")

    flow_called: list[bool] = []

    class _NoTokenAuth:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def has_cached_token(self) -> bool:
            return False

        async def device_code_flow(self) -> dict[str, object]:
            flow_called.append(True)
            return {"access_token": "new-token"}

    mock_graph = MagicMock()
    monkeypatch.setattr("ms365.auth.MS365Auth", _NoTokenAuth)
    monkeypatch.setattr("ms365.graph_client.GraphClient", mock_graph)
    await ms365_server._build_client(None)

    assert flow_called == [True]


@pytest.mark.asyncio
async def test_build_client_exits_when_device_flow_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from ms365.auth import MS365AuthError

    monkeypatch.setenv("MS365_CLIENT_ID", "test-client-id")

    class _FailingAuth:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def has_cached_token(self) -> bool:
            return False

        async def device_code_flow(self) -> dict[str, object]:
            raise MS365AuthError("bad credentials")

    monkeypatch.setattr("ms365.auth.MS365Auth", _FailingAuth)
    with pytest.raises(SystemExit):
        await ms365_server._build_client(None)


# ── _build_client_sync ────────────────────────────────────────────────────────


def test_build_client_sync_exits_without_client_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MS365_CLIENT_ID", raising=False)
    with pytest.raises(SystemExit):
        ms365_server._build_client_sync(None)


def test_build_client_sync_runs_device_flow_when_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    monkeypatch.setenv("MS365_CLIENT_ID", "test-client-id")

    flow_called: list[bool] = []

    class _NoTokenAuth:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def has_cached_token(self) -> bool:
            return False

        def device_code_flow_sync(self) -> dict[str, object]:
            flow_called.append(True)
            return {"access_token": "new-token"}

    mock_graph = MagicMock()
    monkeypatch.setattr("ms365.auth.MS365Auth", _NoTokenAuth)
    monkeypatch.setattr("ms365.graph_client.GraphClient", mock_graph)
    ms365_server._build_client_sync(None)

    assert flow_called == [True]


def test_build_client_sync_exits_when_device_flow_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from ms365.auth import MS365AuthError

    monkeypatch.setenv("MS365_CLIENT_ID", "test-client-id")

    class _FailingAuth:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def has_cached_token(self) -> bool:
            return False

        def device_code_flow_sync(self) -> dict[str, object]:
            raise MS365AuthError("bad credentials")

    monkeypatch.setattr("ms365.auth.MS365Auth", _FailingAuth)
    with pytest.raises(SystemExit):
        ms365_server._build_client_sync(None)


def test_build_client_sync_skips_device_flow_when_token_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    monkeypatch.setenv("MS365_CLIENT_ID", "test-client-id")

    flow_called: list[bool] = []

    class _CachedAuth:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def has_cached_token(self) -> bool:
            return True

        def device_code_flow_sync(self) -> dict[str, object]:
            flow_called.append(True)
            return {"access_token": "new-token"}

    mock_graph = MagicMock()
    monkeypatch.setattr("ms365.auth.MS365Auth", _CachedAuth)
    monkeypatch.setattr("ms365.graph_client.GraphClient", mock_graph)
    ms365_server._build_client_sync(None)

    assert flow_called == []


# ── _async_main ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_async_main_uses_stdio_transport(mock_client: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse

    stdio_called: list[bool] = []

    async def _fake_run_stdio(server: object) -> None:
        stdio_called.append(True)

    monkeypatch.setattr("ms365.ms365_server.run_stdio", _fake_run_stdio)

    args = argparse.Namespace(transport="stdio", services="", read_only=False)
    await ms365_server._async_main(mock_client, args)

    assert stdio_called == [True]


@pytest.mark.asyncio
async def test_async_main_uses_http_transport(mock_client: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse

    http_calls: list[tuple[str, int]] = []

    async def _fake_run_http(server: object, host: str, port: int) -> None:
        http_calls.append((host, port))

    monkeypatch.setattr("ms365.ms365_server.run_http", _fake_run_http)

    args = argparse.Namespace(transport="http", host="0.0.0.0", port=9000, services="", read_only=False)
    await ms365_server._async_main(mock_client, args)

    assert http_calls == [("0.0.0.0", 9000)]


@pytest.mark.asyncio
async def test_async_main_filters_services(mock_client: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse

    captured_specs: list[int] = []

    _orig_build = ms365_server.build_server

    def _fake_build(client: Any, services: list[str] | None = None, read_only: bool = False) -> Any:
        result = _orig_build(client, services=services, read_only=read_only)
        captured_specs.append(len(result[1]))
        return result

    monkeypatch.setattr("ms365.ms365_server.build_server", _fake_build)

    async def _fake_run_stdio(server: object) -> None:
        pass

    monkeypatch.setattr("ms365.ms365_server.run_stdio", _fake_run_stdio)

    args = argparse.Namespace(transport="stdio", services="email,calendar", read_only=False)
    await ms365_server._async_main(mock_client, args)

    assert captured_specs == [_EXPECTED_COUNTS["email"] + _EXPECTED_COUNTS["calendar"]]


# ── run_stdio ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_stdio_calls_run_async() -> None:
    from unittest.mock import AsyncMock, MagicMock

    mock_mcp = MagicMock()
    mock_mcp.run_async = AsyncMock()

    await ms365_server.run_stdio(mock_mcp)

    mock_mcp.run_async.assert_awaited_once_with(transport="stdio", show_banner=False)


# ── _parse_args ───────────────────────────────────────────────────────────────


def test_parse_args_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["ms365-mcp-run"])
    args = ms365_server._parse_args()
    assert args.transport == "stdio"
    assert args.host == ms365_server.DEFAULT_HOST
    assert args.port == ms365_server.DEFAULT_PORT
    assert args.services == ""
    assert args.tenant == ""
    assert args.read_only is False


def test_parse_args_http_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["ms365-mcp-run", "--transport", "http", "--port", "9999"])
    args = ms365_server._parse_args()
    assert args.transport == "http"
    assert args.port == 9999


def test_parse_args_read_only_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["ms365-mcp-run", "--read-only"])
    args = ms365_server._parse_args()
    assert args.read_only is True


# ── call_tool error handling ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_call_tool_handler_exception_raises_tool_error(mock_client: MagicMock) -> None:
    """A Graph API failure surfaces as a `ToolError` (FastMCP's native tool-failure
    signal, `isError=True` on the wire) rather than the old `[Error] ...` text
    disguised as a normal response. See `test_call_unknown_tool_raises` for why
    this is a deliberate, low-risk change rather than a regression."""
    mock_client.get.side_effect = RuntimeError("Graph API down")

    mcp, _ = ms365_server.build_server(mock_client)
    with pytest.raises(ToolError, match="Graph API down"):
        await mcp.call_tool("outlook__list_messages", {"folder": "Inbox", "max_results": 5})


# ── run_http ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_http_calls_uvicorn_serve(mock_client: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    from unittest.mock import AsyncMock, MagicMock

    mcp, _ = ms365_server.build_server(mock_client)

    mock_uvicorn_server = MagicMock()
    mock_uvicorn_server.serve = AsyncMock()

    mock_uvicorn = MagicMock()
    mock_uvicorn.Config = MagicMock(return_value=MagicMock())
    mock_uvicorn.Server = MagicMock(return_value=mock_uvicorn_server)

    monkeypatch.setitem(sys.modules, "uvicorn", mock_uvicorn)

    await ms365_server.run_http(mcp, "127.0.0.1", 9999)

    mock_uvicorn_server.serve.assert_awaited_once()


# ── main ──────────────────────────────────────────────────────────────────────


def test_main_calls_build_client_sync_and_asyncio_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    mock_client = MagicMock()
    sync_called: list[object] = []
    async_called: list[tuple[object, str]] = []

    monkeypatch.setattr("sys.argv", ["ms365-mcp-run"])

    def _fake_build_client_sync(tenant_override: object) -> object:
        sync_called.append(tenant_override)
        return mock_client

    async def _fake_async_main(client: object, args: argparse.Namespace) -> None:
        async_called.append((client, args.transport))

    monkeypatch.setattr("ms365.ms365_server._build_client_sync", _fake_build_client_sync)
    monkeypatch.setattr("ms365.ms365_server._async_main", _fake_async_main)

    ms365_server.main()

    assert sync_called == [None]
    assert len(async_called) == 1
    assert async_called[0][0] is mock_client
    assert async_called[0][1] == "stdio"
