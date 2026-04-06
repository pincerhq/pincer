"""Tests for MS365 integration setup — tool registration and configuration."""

from __future__ import annotations

import pytest

from pincer.integrations.ms365 import register_all_tools
from pincer.integrations.ms365.config import MS365IntegrationConfig, resolve_cache_path
from pincer.tools.registry import ToolRegistry

_MS365_PREFIXES = ("outlook__", "teams__", "onedrive__", "onenote__", "ms_todo__")


@pytest.fixture
def registry_with_tools(mock_client):
    """ToolRegistry with all 69 MS365 tools registered."""
    registry = ToolRegistry()
    count = register_all_tools(registry, mock_client)
    return registry, count


def test_all_69_tools_registered(registry_with_tools):
    """register_all_tools returns 69 and all tools are in the registry."""
    registry, count = registry_with_tools
    assert count == 69
    assert len(registry.list_tools()) == 69


def test_tool_names_prefixed(registry_with_tools):
    """All tool names use a Microsoft service-specific prefix."""
    registry, _ = registry_with_tools
    for name in registry.list_tools():
        assert any(name.startswith(p) for p in _MS365_PREFIXES), (
            f"Tool {name} doesn't have a known MS365 prefix"
        )


def test_read_tools_no_approval(registry_with_tools):
    """Read tools don't require approval."""
    registry, _ = registry_with_tools
    read_tools = [
        "outlook__list_mail_folders", "outlook__list_messages", "outlook__search_messages",
        "outlook__get_message", "outlook__get_message_attachments", "outlook__download_attachment",
        "outlook__list_calendars", "outlook__list_events", "outlook__get_event",
        "outlook__search_events", "outlook__check_availability",
        "onedrive__list_drive_items", "onedrive__search_files", "onedrive__get_file_metadata",
        "onedrive__download_file", "onedrive__get_file_preview", "onedrive__list_shared_with_me",
        "onedrive__list_recent_files",
        "ms_todo__list_task_lists", "ms_todo__list_tasks", "ms_todo__get_task",
        "teams__list_teams", "teams__list_channels", "teams__list_channel_messages",
        "teams__get_channel_message", "teams__list_chats",
        "outlook__list_contacts", "outlook__search_contacts", "outlook__get_contact",
        "onenote__list_notebooks", "onenote__list_sections", "onenote__list_pages",
        "onenote__get_page_content",
    ]
    for name in read_tools:
        assert not registry.requires_approval(name), f"Read tool {name} should not require approval"


def test_write_tools_need_approval(registry_with_tools):
    """Write tools require approval."""
    registry, _ = registry_with_tools
    write_tools = [
        "outlook__send_message", "outlook__reply_to_message", "outlook__reply_all_to_message",
        "outlook__forward_message", "outlook__create_draft", "outlook__update_draft",
        "outlook__send_draft", "outlook__move_message", "outlook__delete_message",
        "outlook__create_event", "outlook__update_event", "outlook__delete_event",
        "outlook__accept_event", "outlook__decline_event", "outlook__tentative_event",
        "outlook__create_online_meeting",
        "onedrive__upload_file", "onedrive__create_folder", "onedrive__move_file",
        "onedrive__rename_file", "onedrive__copy_file", "onedrive__delete_file", "onedrive__share_file",
        "ms_todo__create_task", "ms_todo__update_task", "ms_todo__complete_task",
        "ms_todo__delete_task", "ms_todo__create_task_list",
        "teams__send_channel_message", "teams__send_chat_message",
        "outlook__create_contact", "outlook__update_contact", "outlook__delete_contact",
        "onenote__create_page",
    ]
    for name in write_tools:
        assert registry.requires_approval(name), f"Write tool {name} should require approval"


def test_low_risk_write_tools_no_approval(registry_with_tools):
    """Low-risk write tools (mark_as_read, flag) don't require approval."""
    registry, _ = registry_with_tools
    assert not registry.requires_approval("outlook__mark_as_read")
    assert not registry.requires_approval("outlook__flag_message")


def test_config_default():
    """Default config has common tenant and all services."""
    cfg = MS365IntegrationConfig()
    assert cfg.tenant_id == "common"
    assert cfg.enabled is True
    assert len(cfg.services) == 7


def test_config_resolve_cache_path():
    """Cache path resolves to ~/.pincer/ by default."""
    from pathlib import Path
    cfg = MS365IntegrationConfig()
    path = resolve_cache_path(cfg)
    assert path == Path.home() / ".pincer" / "ms365_token_cache.json"


def test_config_custom_cache_path():
    """Custom cache path in config is used."""
    from pathlib import Path
    cfg = MS365IntegrationConfig(token_cache_path="/tmp/custom_tokens.json")
    path = resolve_cache_path(cfg)
    assert path == Path("/tmp/custom_tokens.json")
