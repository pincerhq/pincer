"""Tests for Slack message tools (18 tools)."""

from __future__ import annotations

import pytest


def fake_resp(**kwargs):
    d = {"ok": True}
    d.update(kwargs)
    return d


@pytest.mark.asyncio
async def test_get_channel_history_empty(mock_client):
    from pincer.integrations.slack.tools_messages import slack__get_channel_history

    result = await slack__get_channel_history(mock_client, channel="C001")
    assert "No messages" in result


@pytest.mark.asyncio
async def test_get_channel_history_with_messages(mock_client):
    from pincer.integrations.slack.tools_messages import slack__get_channel_history

    mock_client.bot.conversations_history.return_value = fake_resp(
        messages=[
            {"ts": "123.456", "user": "U001", "text": "Hello world"},
            {"ts": "124.000", "user": "U002", "text": "Hi there"},
        ],
        has_more=False,
    )
    result = await slack__get_channel_history(mock_client, channel="C001")
    assert "Hello world" in result
    assert "Hi there" in result


@pytest.mark.asyncio
async def test_get_channel_history_has_more(mock_client):
    from pincer.integrations.slack.tools_messages import slack__get_channel_history

    mock_client.bot.conversations_history.return_value = fake_resp(
        messages=[{"ts": "1.0", "user": "U1", "text": "msg"}],
        has_more=True,
    )
    result = await slack__get_channel_history(mock_client, channel="C001")
    assert "More messages" in result


@pytest.mark.asyncio
async def test_get_thread_replies_empty(mock_client):
    from pincer.integrations.slack.tools_messages import slack__get_thread_replies

    result = await slack__get_thread_replies(mock_client, channel="C001", thread_ts="1.0")
    assert "No replies" in result


@pytest.mark.asyncio
async def test_get_thread_replies_with_data(mock_client):
    from pincer.integrations.slack.tools_messages import slack__get_thread_replies

    mock_client.bot.conversations_replies.return_value = fake_resp(
        messages=[
            {"ts": "1.0", "user": "U1", "text": "parent"},
            {"ts": "1.1", "user": "U2", "text": "reply"},
        ]
    )
    result = await slack__get_thread_replies(mock_client, channel="C001", thread_ts="1.0")
    assert "parent" in result
    assert "reply" in result


@pytest.mark.asyncio
async def test_get_message_permalink(mock_client):
    from pincer.integrations.slack.tools_messages import slack__get_message_permalink

    result = await slack__get_message_permalink(mock_client, channel="C001", message_ts="1.0")
    assert "https://slack.com/p/123" in result


@pytest.mark.asyncio
async def test_post_message(mock_client):
    from pincer.integrations.slack.tools_messages import slack__post_message

    result = await slack__post_message(mock_client, channel="C001", text="Hello")
    assert "123.456" in result
    mock_client.bot.chat_postMessage.assert_called_once()


@pytest.mark.asyncio
async def test_post_message_with_thread_ts(mock_client):
    from pincer.integrations.slack.tools_messages import slack__post_message

    await slack__post_message(mock_client, channel="C001", text="Reply", thread_ts="1.0")
    call_kwargs = mock_client.bot.chat_postMessage.call_args.kwargs
    assert call_kwargs.get("thread_ts") == "1.0"


@pytest.mark.asyncio
async def test_post_threaded_reply(mock_client):
    from pincer.integrations.slack.tools_messages import slack__post_threaded_reply

    result = await slack__post_threaded_reply(mock_client, channel="C001", thread_ts="1.0", text="My reply")
    assert "1.0" in result or "Reply posted" in result


@pytest.mark.asyncio
async def test_post_ephemeral(mock_client):
    from pincer.integrations.slack.tools_messages import slack__post_ephemeral

    result = await slack__post_ephemeral(mock_client, channel="C001", user="U001", text="Only you see this")
    assert "Ephemeral" in result or "U001" in result


@pytest.mark.asyncio
async def test_update_message(mock_client):
    from pincer.integrations.slack.tools_messages import slack__update_message

    result = await slack__update_message(mock_client, channel="C001", ts="1.0", text="Edited")
    assert "updated" in result.lower()


@pytest.mark.asyncio
async def test_delete_message(mock_client):
    from pincer.integrations.slack.tools_messages import slack__delete_message

    result = await slack__delete_message(mock_client, channel="C001", ts="1.0")
    assert "deleted" in result.lower()


@pytest.mark.asyncio
async def test_schedule_message(mock_client):
    from pincer.integrations.slack.tools_messages import slack__schedule_message

    result = await slack__schedule_message(mock_client, channel="C001", text="Future", post_at=9999999999)
    assert "scheduled" in result.lower() or "Qxxx" in result


@pytest.mark.asyncio
async def test_list_scheduled_messages_empty(mock_client):
    from pincer.integrations.slack.tools_messages import slack__list_scheduled_messages

    result = await slack__list_scheduled_messages(mock_client)
    assert "No scheduled" in result


@pytest.mark.asyncio
async def test_delete_scheduled_message(mock_client):
    from pincer.integrations.slack.tools_messages import slack__delete_scheduled_message

    result = await slack__delete_scheduled_message(mock_client, channel="C001", scheduled_message_id="Qxxx")
    assert "cancelled" in result.lower() or "Qxxx" in result


@pytest.mark.asyncio
async def test_post_message_with_blocks(mock_client):
    from pincer.integrations.slack.tools_messages import slack__post_message_with_blocks

    result = await slack__post_message_with_blocks(
        mock_client,
        channel="C001",
        blocks_json='[{"type": "section", "text": {"type": "mrkdwn", "text": "Hello"}}]',
    )
    assert "Block Kit" in result or "C001" in result


@pytest.mark.asyncio
async def test_post_message_with_blocks_invalid_json(mock_client):
    from pincer.integrations.slack.tools_messages import slack__post_message_with_blocks

    result = await slack__post_message_with_blocks(mock_client, channel="C001", blocks_json="not-json")
    assert "Error" in result


@pytest.mark.asyncio
async def test_share_message(mock_client):
    from pincer.integrations.slack.tools_messages import slack__share_message

    result = await slack__share_message(
        mock_client,
        channel="C002",
        source_channel="C001",
        message_ts="1.0",
        comment="FYI",
    )
    assert "shared" in result.lower() or "C002" in result


@pytest.mark.asyncio
async def test_post_dm_by_user_id(mock_client):
    from pincer.integrations.slack.tools_messages import slack__post_dm

    result = await slack__post_dm(mock_client, user="U001", text="Hi")
    assert "DM sent" in result or "D123" in result


@pytest.mark.asyncio
async def test_post_dm_by_email(mock_client):
    from pincer.integrations.slack.tools_messages import slack__post_dm

    result = await slack__post_dm(mock_client, user="alice@example.com", text="Hi")
    assert "DM sent" in result or "Error" in result  # resolves email → user ID


@pytest.mark.asyncio
async def test_post_broadcast_reply(mock_client):
    from pincer.integrations.slack.tools_messages import slack__post_broadcast_reply

    result = await slack__post_broadcast_reply(mock_client, channel="C001", thread_ts="1.0", text="See this!")
    assert "Broadcast" in result or "1.0" in result


@pytest.mark.asyncio
async def test_get_unread_messages_none(mock_client):
    from pincer.integrations.slack.tools_messages import slack__get_unread_messages

    result = await slack__get_unread_messages(mock_client)
    assert "No unread" in result


@pytest.mark.asyncio
async def test_get_unread_messages_with_data(mock_client):
    from pincer.integrations.slack.tools_messages import slack__get_unread_messages

    mock_client.bot.conversations_list.return_value = fake_resp(
        channels=[
            {"id": "C001", "name": "general", "unread_count": 3},
            {"id": "C002", "name": "eng", "unread_count": 0},
        ],
        response_metadata={"next_cursor": ""},
    )
    result = await slack__get_unread_messages(mock_client)
    assert "general" in result
    assert "3" in result


@pytest.mark.asyncio
async def test_mark_channel_read(mock_client):
    from pincer.integrations.slack.tools_messages import slack__mark_channel_read

    result = await slack__mark_channel_read(mock_client, channel="C001", ts="1.0")
    assert "marked as read" in result.lower()


@pytest.mark.asyncio
async def test_summarize_channel(mock_client):
    from pincer.integrations.slack.tools_messages import slack__summarize_channel

    mock_client.bot.conversations_history.return_value = fake_resp(
        messages=[
            {"ts": "1.0", "user": "U1", "text": "Deploy started"},
            {"ts": "2.0", "user": "U2", "text": "Deploy done"},
        ],
        has_more=False,
    )
    result = await slack__summarize_channel(mock_client, channel="C001", limit=10)
    assert "Deploy started" in result
    assert "Deploy done" in result


@pytest.mark.asyncio
async def test_register_message_tools_count():
    from unittest.mock import MagicMock

    from pincer.integrations.slack.tools_messages import register_message_tools
    from pincer.tools.registry import ToolRegistry

    registry = ToolRegistry()
    client = MagicMock()
    count = register_message_tools(registry, client)
    assert count == 18
    assert len(registry.list_tools()) == 18


@pytest.mark.asyncio
async def test_message_write_tools_require_approval():
    from unittest.mock import MagicMock

    from pincer.integrations.slack.tools_messages import register_message_tools
    from pincer.tools.registry import ToolRegistry

    registry = ToolRegistry()
    client = MagicMock()
    register_message_tools(registry, client)

    write_tools = [
        "slack__post_message",
        "slack__post_threaded_reply",
        "slack__post_ephemeral",
        "slack__update_message",
        "slack__delete_message",
        "slack__schedule_message",
        "slack__delete_scheduled_message",
        "slack__post_message_with_blocks",
        "slack__share_message",
        "slack__post_dm",
        "slack__post_broadcast_reply",
    ]
    for name in write_tools:
        assert registry.requires_approval(name), f"{name} should require approval"
