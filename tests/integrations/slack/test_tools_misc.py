"""Tests for Slack misc tools — pins, bookmarks, reminders, search (12 tools)."""

from __future__ import annotations

import pytest


def fake_resp(**kwargs):
    d = {'ok': True}
    d.update(kwargs)
    return d


# ── pins ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pin_message(mock_client):
    from pincer.integrations.slack.tools_misc import slack__pin_message

    result = await slack__pin_message(mock_client, channel="C001", timestamp="1.0")
    assert "pinned" in result.lower()


@pytest.mark.asyncio
async def test_unpin_message(mock_client):
    from pincer.integrations.slack.tools_misc import slack__unpin_message

    result = await slack__unpin_message(mock_client, channel="C001", timestamp="1.0")
    assert "unpinned" in result.lower()


@pytest.mark.asyncio
async def test_list_pins_empty(mock_client):
    from pincer.integrations.slack.tools_misc import slack__list_pins

    result = await slack__list_pins(mock_client, channel="C001")
    assert "No pinned" in result


@pytest.mark.asyncio
async def test_list_pins_with_data(mock_client):
    from pincer.integrations.slack.tools_misc import slack__list_pins

    mock_client.bot.pins_list.return_value = fake_resp(
        items=[{"type": "message", "message": {"ts": "1.0", "text": "Important!"}}]
    )
    result = await slack__list_pins(mock_client, channel="C001")
    assert "Important!" in result


# ── bookmarks ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_bookmark(mock_client):
    from pincer.integrations.slack.tools_misc import slack__add_bookmark

    result = await slack__add_bookmark(
        mock_client, channel_id="C001", title="Docs", link="https://docs.example.com"
    )
    assert "added" in result.lower() or "Bk123" in result


@pytest.mark.asyncio
async def test_list_bookmarks_empty(mock_client):
    from pincer.integrations.slack.tools_misc import slack__list_bookmarks

    result = await slack__list_bookmarks(mock_client, channel_id="C001")
    assert "No bookmarks" in result


@pytest.mark.asyncio
async def test_list_bookmarks_with_data(mock_client):
    from pincer.integrations.slack.tools_misc import slack__list_bookmarks

    mock_client.bot.bookmarks_list.return_value = fake_resp(
        bookmarks=[{"id": "Bk001", "title": "Runbook", "link": "https://wiki.example.com"}]
    )
    result = await slack__list_bookmarks(mock_client, channel_id="C001")
    assert "Runbook" in result


@pytest.mark.asyncio
async def test_remove_bookmark(mock_client):
    from pincer.integrations.slack.tools_misc import slack__remove_bookmark

    result = await slack__remove_bookmark(mock_client, channel_id="C001", bookmark_id="Bk001")
    assert "removed" in result.lower()


# ── reminders ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_reminder(mock_client):
    from pincer.integrations.slack.tools_misc import slack__create_reminder

    mock_client.bot.reminders_add.return_value = fake_resp(
        reminder={"id": "Rm123", "text": "Follow up", "time": 9999}
    )
    result = await slack__create_reminder(
        mock_client, text="Follow up", time="tomorrow 9am"
    )
    assert "Reminder created" in result or "Rm123" in result
    assert "Follow up" in result


@pytest.mark.asyncio
async def test_list_reminders_empty(mock_client):
    from pincer.integrations.slack.tools_misc import slack__list_reminders

    result = await slack__list_reminders(mock_client)
    assert "No reminders" in result


@pytest.mark.asyncio
async def test_list_reminders_with_data(mock_client):
    from pincer.integrations.slack.tools_misc import slack__list_reminders

    mock_client.bot.reminders_list.return_value = fake_resp(
        reminders=[
            {"id": "Rm001", "text": "Review PR", "time": 9999999, "complete_ts": None}
        ]
    )
    result = await slack__list_reminders(mock_client)
    assert "Review PR" in result


@pytest.mark.asyncio
async def test_complete_reminder(mock_client):
    from pincer.integrations.slack.tools_misc import slack__complete_reminder

    result = await slack__complete_reminder(mock_client, reminder="Rm001")
    assert "complete" in result.lower()


@pytest.mark.asyncio
async def test_delete_reminder(mock_client):
    from pincer.integrations.slack.tools_misc import slack__delete_reminder

    result = await slack__delete_reminder(mock_client, reminder="Rm001")
    assert "deleted" in result.lower()


# ── search ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_messages_empty(mock_client):
    from pincer.integrations.slack.tools_misc import slack__search_messages

    result = await slack__search_messages(mock_client, query="deployment")
    assert "No messages found" in result


@pytest.mark.asyncio
async def test_search_messages_with_results(mock_client):
    from pincer.integrations.slack.tools_misc import slack__search_messages

    mock_client.user.search_messages.return_value = fake_resp(
        messages={
            "matches": [
                {"channel": {"name": "engineering"}, "ts": "1.0", "text": "Deploy done"},
            ],
            "total": 1,
        }
    )
    result = await slack__search_messages(mock_client, query="deploy")
    assert "Deploy done" in result
    assert "engineering" in result


@pytest.mark.asyncio
async def test_search_files_empty(mock_client):
    from pincer.integrations.slack.tools_misc import slack__search_files

    result = await slack__search_files(mock_client, query="report")
    assert "No files found" in result


@pytest.mark.asyncio
async def test_search_files_with_results(mock_client):
    from pincer.integrations.slack.tools_misc import slack__search_files

    mock_client.user.search_files.return_value = fake_resp(
        files={
            "matches": [
                {"id": "F001", "name": "report.pdf", "pretty_type": "PDF"},
            ],
            "total": 1,
        }
    )
    result = await slack__search_files(mock_client, query="report")
    assert "report.pdf" in result


# ── registration ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_misc_tools_count():
    from unittest.mock import MagicMock

    from pincer.integrations.slack.tools_misc import register_misc_tools
    from pincer.tools.registry import ToolRegistry

    registry = ToolRegistry()
    client = MagicMock()
    count = register_misc_tools(registry, client)
    assert count == 12
    assert len(registry.list_tools()) == 12


@pytest.mark.asyncio
async def test_misc_write_tools_require_approval():
    from unittest.mock import MagicMock

    from pincer.integrations.slack.tools_misc import register_misc_tools
    from pincer.tools.registry import ToolRegistry

    registry = ToolRegistry()
    client = MagicMock()
    register_misc_tools(registry, client)

    write_tools = [
        "slack__pin_message",
        "slack__unpin_message",
        "slack__add_bookmark",
        "slack__remove_bookmark",
        "slack__create_reminder",
        "slack__delete_reminder",
    ]
    for name in write_tools:
        assert registry.requires_approval(name), f"{name} should require approval"


@pytest.mark.asyncio
async def test_misc_read_tools_no_approval():
    from unittest.mock import MagicMock

    from pincer.integrations.slack.tools_misc import register_misc_tools
    from pincer.tools.registry import ToolRegistry

    registry = ToolRegistry()
    client = MagicMock()
    register_misc_tools(registry, client)

    for name in ["slack__list_pins", "slack__list_bookmarks", "slack__list_reminders",
                 "slack__complete_reminder", "slack__search_messages", "slack__search_files"]:
        assert not registry.requires_approval(name), f"{name} should NOT require approval"
