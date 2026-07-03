"""Tests for the standalone MS365 MCP server."""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, Any

import pytest
from cryptography.fernet import Fernet
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


def test_collect_all_tools_count(resolve_client: Any) -> None:
    specs = ms365_server.collect_tools(resolve_client)
    assert len(specs) == _TOTAL


def test_collect_per_service_counts(resolve_client: Any) -> None:
    for service, expected in _EXPECTED_COUNTS.items():
        specs = ms365_server.collect_tools(resolve_client, services=[service])
        assert len(specs) == expected, f"{service}: expected {expected}, got {len(specs)}"


def test_unique_names_and_nonempty_schemas(resolve_client: Any) -> None:
    specs = ms365_server.collect_tools(resolve_client)
    names = [s.name for s in specs]
    assert len(names) == len(set(names)), "tool names must be unique"
    for s in specs:
        assert s.description, f"{s.name} missing description"
        assert s.parameters.get("type") == "object", f"{s.name} schema not an object"


def test_read_only_drops_write_tools(resolve_client: Any) -> None:
    full = ms365_server.collect_tools(resolve_client)
    read_only = ms365_server.collect_tools(resolve_client, read_only=True)
    assert len(read_only) < len(full)
    assert all(not s.require_approval for s in read_only)
    ro_names = {s.name for s in read_only}
    assert "outlook__send_message" not in ro_names
    assert "outlook__list_messages" in ro_names


def test_unknown_service_skipped(resolve_client: Any) -> None:
    specs = ms365_server.collect_tools(resolve_client, services=["email", "does_not_exist"])
    assert len(specs) == _EXPECTED_COUNTS["email"]


def test_write_tools_have_approval_flag(resolve_client: Any) -> None:
    specs = ms365_server.collect_tools(resolve_client)
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


def test_read_tools_no_approval_flag(resolve_client: Any) -> None:
    specs = ms365_server.collect_tools(resolve_client)
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


def test_low_risk_write_tools_no_approval(resolve_client: Any) -> None:
    specs = ms365_server.collect_tools(resolve_client)
    by_name = {s.name: s for s in specs}
    assert not by_name["outlook__mark_as_read"].require_approval
    assert not by_name["outlook__flag_message"].require_approval


# ── build_server ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_server_lists_all_tools(resolve_client: Any) -> None:
    mcp, specs = ms365_server.build_server(resolve_client)
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
async def test_build_server_schema_matches_spec_parameters(resolve_client: Any) -> None:
    """FastMCP derives each tool's JSON schema from the handler's real signature —
    confirm it lines up with the `parameters` dict `collect_tools()` still carries
    (and, crucially, doesn't leak the bound `client: GraphClient` or `ctx: Context`
    arguments)."""
    mcp, _ = ms365_server.build_server(resolve_client)
    tool = await mcp.get_tool("outlook__list_messages")
    assert tool is not None
    schema = tool.parameters
    assert "client" not in schema["properties"]
    assert "ctx" not in schema["properties"]
    assert schema["properties"]["folder"]["default"] == "Inbox"
    assert schema["properties"]["max_results"]["default"] == 25


@pytest.mark.asyncio
async def test_call_tool_dispatches_to_handler(resolve_client: Any, mock_client: MagicMock) -> None:
    mcp, _ = ms365_server.build_server(resolve_client)
    result = await mcp.call_tool("outlook__list_messages", {"folder": "Inbox", "max_results": 5})
    content = result.content
    assert content and content[0].type == "text"
    mock_client.get.assert_awaited()


@pytest.mark.asyncio
async def test_call_tool_uses_client_resolved_for_the_caller_identity(mock_client: MagicMock) -> None:
    """Two different identities resolve to two different GraphClients."""
    from unittest.mock import AsyncMock
    from unittest.mock import MagicMock as _MagicMock

    other_client = _MagicMock()
    other_client.get = AsyncMock(return_value={"value": []})

    async def resolve_client(ctx: Any) -> Any:
        request_context = ctx.request_context
        meta = getattr(request_context, "meta", None) if request_context is not None else None
        identity = getattr(meta, "identity", None) if meta is not None else None
        return other_client if identity == "usr_other" else mock_client

    mcp, _ = ms365_server.build_server(resolve_client)

    await mcp.call_tool("outlook__list_messages", {"folder": "Inbox", "max_results": 5})
    mock_client.get.assert_awaited()
    other_client.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_call_unknown_tool_raises(resolve_client: Any) -> None:
    """FastMCP raises for an unregistered tool name instead of returning
    "[Error] Unknown tool: ..." as ordinary text — a deliberate behavior change
    from the low-level SDK version. A real MCP client sees this as a protocol
    error result (`isError=True`) rather than a disguised successful response;
    Pincer's own bridge (`pincer/mcp/bridge.py::_format_result`) already extracts
    the text content regardless of `isError`, so the LLM still sees a clear
    error string either way."""
    mcp, _ = ms365_server.build_server(resolve_client)
    with pytest.raises(NotFoundError, match="nope__nope"):
        await mcp.call_tool("nope__nope", {})


# ── build_http_app ─────────────────────────────────────────────────────────────


def test_build_http_app_health_endpoint(resolve_client: Any) -> None:
    from starlette.testclient import TestClient

    mcp, _ = ms365_server.build_server(resolve_client)
    app = ms365_server.build_http_app(mcp)

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["service"] == "ms365-mcp"


def test_build_http_app_root_endpoint(resolve_client: Any) -> None:
    from starlette.testclient import TestClient

    mcp, _ = ms365_server.build_server(resolve_client)
    app = ms365_server.build_http_app(mcp)

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_build_http_app_has_mcp_route(resolve_client: Any) -> None:
    mcp, _ = ms365_server.build_server(resolve_client)
    app = ms365_server.build_http_app(mcp)
    route_paths = [r.path for r in app.routes]
    assert "/mcp" in route_paths


# ── _identity_from_ctx ────────────────────────────────────────────────────────


def test_identity_from_ctx_reads_meta() -> None:
    from unittest.mock import MagicMock

    ctx = MagicMock()
    ctx.request_context.meta.identity = "usr_abc123"
    assert ms365_server._identity_from_ctx(ctx) == "usr_abc123"


def test_identity_from_ctx_missing_request_context_returns_none() -> None:
    from unittest.mock import MagicMock

    ctx = MagicMock()
    ctx.request_context = None
    assert ms365_server._identity_from_ctx(ctx) is None


def test_identity_from_ctx_missing_meta_returns_none() -> None:
    from unittest.mock import MagicMock

    ctx = MagicMock()
    ctx.request_context.meta = None
    assert ms365_server._identity_from_ctx(ctx) is None


def test_identity_from_ctx_meta_without_identity_returns_none() -> None:
    from unittest.mock import MagicMock

    ctx = MagicMock(spec=["request_context"])
    ctx.request_context = MagicMock(spec=["meta"])
    ctx.request_context.meta = MagicMock(spec=[])  # no `identity` attribute
    assert ms365_server._identity_from_ctx(ctx) is None


# ── _make_client_resolver ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_make_client_resolver_delegates_to_manager() -> None:
    from unittest.mock import AsyncMock, MagicMock

    sentinel_client = MagicMock()
    manager = MagicMock()
    manager.get_or_create = AsyncMock(return_value=sentinel_client)

    ctx = MagicMock()
    ctx.request_context.meta.identity = "usr_abc"

    resolve_client = ms365_server._make_client_resolver(manager)
    client = await resolve_client(ctx)

    assert client is sentinel_client
    manager.get_or_create.assert_awaited_once_with("usr_abc", ctx=ctx)


# ── _register_auth_status_tool ────────────────────────────────────────────────


def _status_manager(status: Any) -> Any:
    """A minimal IdentitySessionManager stand-in exposing only `status_for`."""
    from unittest.mock import MagicMock

    manager = MagicMock()
    manager.status_for.return_value = status
    return manager


@pytest.mark.asyncio
async def test_check_auth_status_tool_reports_manager_status(resolve_client: Any) -> None:
    from ms365.identity_session import AuthStatus

    manager = _status_manager(AuthStatus("signed_in", "Signed in to Microsoft 365."))

    mcp, _ = ms365_server.build_server(resolve_client, services=["email"])
    ms365_server._register_auth_status_tool(mcp, manager)

    result = await mcp.call_tool("ms365__check_auth_status", {})
    content = result.content[0]
    assert content.type == "text"
    assert "Signed in to Microsoft 365." in content.text


@pytest.mark.asyncio
async def test_check_auth_status_tool_not_in_public_schema_args(resolve_client: Any) -> None:
    from ms365.identity_session import AuthStatus

    manager = _status_manager(AuthStatus("not_signed_in", "Not signed in yet."))

    mcp, _ = ms365_server.build_server(resolve_client, services=["email"])
    ms365_server._register_auth_status_tool(mcp, manager)

    tool = await mcp.get_tool("ms365__check_auth_status")
    assert tool is not None
    assert tool.parameters["properties"] == {}


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


# ── _async_main ───────────────────────────────────────────────────────────────


def _set_dummy_encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid load_or_generate_fernet() writing a real key file to disk during tests."""
    monkeypatch.setenv("MS365_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())


@pytest.mark.asyncio
async def test_async_main_exits_without_client_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MS365_CLIENT_ID", raising=False)
    args = argparse.Namespace(transport="stdio", services="", read_only=False, tenant="")

    with pytest.raises(SystemExit):
        await ms365_server._async_main(args)


@pytest.mark.asyncio
async def test_async_main_uses_stdio_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MS365_CLIENT_ID", "test-client-id")
    _set_dummy_encryption_key(monkeypatch)

    stdio_called: list[bool] = []

    async def _fake_run_stdio(server: object) -> None:
        stdio_called.append(True)

    monkeypatch.setattr("ms365.ms365_server.run_stdio", _fake_run_stdio)

    args = argparse.Namespace(transport="stdio", services="", read_only=False, tenant="")
    await ms365_server._async_main(args)

    assert stdio_called == [True]


@pytest.mark.asyncio
async def test_async_main_uses_http_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MS365_CLIENT_ID", "test-client-id")
    _set_dummy_encryption_key(monkeypatch)

    http_calls: list[tuple[str, int]] = []

    async def _fake_run_http(server: object, host: str, port: int) -> None:
        http_calls.append((host, port))

    monkeypatch.setattr("ms365.ms365_server.run_http", _fake_run_http)

    args = argparse.Namespace(transport="http", host="0.0.0.0", port=9000, services="", read_only=False, tenant="")
    await ms365_server._async_main(args)

    assert http_calls == [("0.0.0.0", 9000)]


@pytest.mark.asyncio
async def test_async_main_filters_services(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MS365_CLIENT_ID", "test-client-id")
    _set_dummy_encryption_key(monkeypatch)

    captured_specs: list[int] = []

    _orig_build = ms365_server.build_server

    def _fake_build(resolve_client: Any, services: list[str] | None = None, read_only: bool = False) -> Any:
        result = _orig_build(resolve_client, services=services, read_only=read_only)
        captured_specs.append(len(result[1]))
        return result

    monkeypatch.setattr("ms365.ms365_server.build_server", _fake_build)

    async def _fake_run_stdio(server: object) -> None:
        pass

    monkeypatch.setattr("ms365.ms365_server.run_stdio", _fake_run_stdio)

    args = argparse.Namespace(transport="stdio", services="email,calendar", read_only=False, tenant="")
    await ms365_server._async_main(args)

    assert captured_specs == [_EXPECTED_COUNTS["email"] + _EXPECTED_COUNTS["calendar"]]


@pytest.mark.asyncio
async def test_async_main_builds_identity_session_manager_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """cfg.services (not the --services CLI filter) drives the OAuth scope set,
    matching pre-refactor behavior where --services only hid tools."""
    from unittest.mock import MagicMock

    monkeypatch.setenv("MS365_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("MS365_SERVICES", "email,calendar,onedrive")
    _set_dummy_encryption_key(monkeypatch)

    constructed: list[dict[str, Any]] = []

    class _CapturingManager:
        def __init__(self, **kwargs: Any) -> None:
            constructed.append(kwargs)

        async def get_or_create(self, identity: str | None) -> MagicMock:
            return MagicMock()

    monkeypatch.setattr("ms365.identity_session.IdentitySessionManager", _CapturingManager)

    async def _fake_run_stdio(server: object) -> None:
        pass

    monkeypatch.setattr("ms365.ms365_server.run_stdio", _fake_run_stdio)

    args = argparse.Namespace(transport="stdio", services="email", read_only=False, tenant="")
    await ms365_server._async_main(args)

    assert len(constructed) == 1
    assert constructed[0]["services"] == ["email", "calendar", "onedrive"]


# ── run_stdio ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_stdio_calls_run_async() -> None:
    from unittest.mock import AsyncMock, MagicMock

    mock_mcp = MagicMock()
    mock_mcp.run_async = AsyncMock()

    await ms365_server.run_stdio(mock_mcp)

    mock_mcp.run_async.assert_awaited_once_with(transport="stdio", show_banner=False)


# ── call_tool error handling ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_call_tool_handler_exception_raises_tool_error(resolve_client: Any, mock_client: MagicMock) -> None:
    """A Graph API failure surfaces as a `ToolError` (FastMCP's native tool-failure
    signal, `isError=True` on the wire) rather than the old `[Error] ...` text
    disguised as a normal response. See `test_call_unknown_tool_raises` for why
    this is a deliberate, low-risk change rather than a regression."""
    mock_client.get.side_effect = RuntimeError("Graph API down")

    mcp, _ = ms365_server.build_server(resolve_client)
    with pytest.raises(ToolError, match="Graph API down"):
        await mcp.call_tool("outlook__list_messages", {"folder": "Inbox", "max_results": 5})


# ── run_http ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_http_calls_uvicorn_serve(resolve_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    from unittest.mock import AsyncMock, MagicMock

    mcp, _ = ms365_server.build_server(resolve_client)

    mock_uvicorn_server = MagicMock()
    mock_uvicorn_server.serve = AsyncMock()

    mock_uvicorn = MagicMock()
    mock_uvicorn.Config = MagicMock(return_value=MagicMock())
    mock_uvicorn.Server = MagicMock(return_value=mock_uvicorn_server)

    monkeypatch.setitem(sys.modules, "uvicorn", mock_uvicorn)

    await ms365_server.run_http(mcp, "127.0.0.1", 9999)

    mock_uvicorn_server.serve.assert_awaited_once()


# ── main ──────────────────────────────────────────────────────────────────────


def test_main_calls_async_main_via_asyncio_run(monkeypatch: pytest.MonkeyPatch) -> None:
    async_called: list[str] = []

    monkeypatch.setattr("sys.argv", ["ms365-mcp-run"])

    async def _fake_async_main(args: argparse.Namespace) -> None:
        async_called.append(args.transport)

    monkeypatch.setattr("ms365.ms365_server._async_main", _fake_async_main)

    ms365_server.main()

    assert async_called == ["stdio"]
