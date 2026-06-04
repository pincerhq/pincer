"""Tests for the standalone MS365 MCP server."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
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
}
_TOTAL = sum(_EXPECTED_COUNTS.values())  # 61


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
    import mcp.types as types

    server, specs = ms365_server.build_server(mock_client)
    assert len(specs) == _TOTAL

    list_handler = server.request_handlers[types.ListToolsRequest]
    result = await list_handler(types.ListToolsRequest(method="tools/list"))
    tools = result.root.tools
    assert len(tools) == _TOTAL

    by_name = {t.name: t for t in tools}
    assert by_name["outlook__send_message"].annotations.destructiveHint is True
    assert by_name["outlook__list_messages"].annotations.readOnlyHint is True


@pytest.mark.asyncio
async def test_call_tool_dispatches_to_handler(mock_client: MagicMock) -> None:
    import mcp.types as types

    server, _ = ms365_server.build_server(mock_client)
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
async def test_call_unknown_tool_returns_error(mock_client: MagicMock) -> None:
    import mcp.types as types

    server, _ = ms365_server.build_server(mock_client)
    call_handler = server.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name="nope__nope", arguments={}),
    )
    result = await call_handler(req)
    assert "Unknown tool" in result.root.content[0].text


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
