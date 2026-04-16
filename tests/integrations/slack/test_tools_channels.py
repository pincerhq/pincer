"""Tests for Slack channel tools (16 tools)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def fake_resp(**kwargs):
    d = {'ok': True}
    d.update(kwargs)
    return d


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def channels_with_data(mock_bot):
    mock_bot.conversations_list.return_value = fake_resp(
        channels=[
            {"id": "C001", "name": "general", "num_members": 10, "is_private": False, "is_archived": False},
            {"id": "C002", "name": "engineering", "num_members": 5, "is_private": True, "is_archived": False},
        ],
        response_metadata={"next_cursor": ""},
    )
    return mock_bot


# ── tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_channels_empty(mock_client):
    from pincer.integrations.slack.tools_channels import slack__list_channels

    result = await slack__list_channels(mock_client)
    assert "No channels found" in result


@pytest.mark.asyncio
async def test_list_channels_with_data(mock_client, channels_with_data):
    from pincer.integrations.slack.tools_channels import slack__list_channels

    result = await slack__list_channels(mock_client)
    assert "general" in result
    assert "engineering" in result
    assert "C001" in result


@pytest.mark.asyncio
async def test_get_channel_info(mock_client):
    from pincer.integrations.slack.tools_channels import slack__get_channel_info

    mock_client.bot.conversations_info.return_value = fake_resp(
        channel={
            "id": "C001",
            "name": "general",
            "is_private": False,
            "num_members": 42,
            "topic": {"value": "General chat"},
            "purpose": {"value": "For everyone"},
            "is_archived": False,
            "created": 1000000,
        }
    )
    result = await slack__get_channel_info(mock_client, channel="C001")
    assert "general" in result
    assert "42" in result
    assert "General chat" in result


@pytest.mark.asyncio
async def test_list_channel_members(mock_client):
    from pincer.integrations.slack.tools_channels import slack__list_channel_members

    mock_client.bot.conversations_members.return_value = fake_resp(
        members=["U001", "U002", "U003"],
        response_metadata={"next_cursor": ""},
    )
    result = await slack__list_channel_members(mock_client, channel="C001")
    assert "U001" in result
    assert "3" in result


@pytest.mark.asyncio
async def test_create_channel(mock_client):
    from pincer.integrations.slack.tools_channels import slack__create_channel

    result = await slack__create_channel(mock_client, name="new-channel")
    assert "test" in result or "Created" in result


@pytest.mark.asyncio
async def test_create_channel_private(mock_client):
    from pincer.integrations.slack.tools_channels import slack__create_channel

    mock_client.bot.conversations_create.return_value = fake_resp(
        channel={"id": "C999", "name": "secret"}
    )
    result = await slack__create_channel(mock_client, name="secret", is_private=True)
    assert "private" in result


@pytest.mark.asyncio
async def test_archive_channel(mock_client):
    from pincer.integrations.slack.tools_channels import slack__archive_channel

    result = await slack__archive_channel(mock_client, channel="C001")
    assert "archived" in result.lower()
    mock_client.bot.conversations_archive.assert_called_once_with(channel="C001")


@pytest.mark.asyncio
async def test_unarchive_channel(mock_client):
    from pincer.integrations.slack.tools_channels import slack__unarchive_channel

    result = await slack__unarchive_channel(mock_client, channel="C001")
    assert "unarchived" in result.lower()


@pytest.mark.asyncio
async def test_rename_channel(mock_client):
    from pincer.integrations.slack.tools_channels import slack__rename_channel

    result = await slack__rename_channel(mock_client, channel="C001", name="new-name")
    assert "new-name" in result


@pytest.mark.asyncio
async def test_set_channel_topic(mock_client):
    from pincer.integrations.slack.tools_channels import slack__set_channel_topic

    result = await slack__set_channel_topic(mock_client, channel="C001", topic="Hello")
    assert "Hello" in result


@pytest.mark.asyncio
async def test_set_channel_purpose(mock_client):
    from pincer.integrations.slack.tools_channels import slack__set_channel_purpose

    result = await slack__set_channel_purpose(mock_client, channel="C001", purpose="We build things")
    assert "We build things" in result


@pytest.mark.asyncio
async def test_join_channel(mock_client):
    from pincer.integrations.slack.tools_channels import slack__join_channel

    result = await slack__join_channel(mock_client, channel="C001")
    assert "Joined" in result or "general" in result


@pytest.mark.asyncio
async def test_leave_channel(mock_client):
    from pincer.integrations.slack.tools_channels import slack__leave_channel

    result = await slack__leave_channel(mock_client, channel="C001")
    assert "Left" in result


@pytest.mark.asyncio
async def test_invite_to_channel(mock_client):
    from pincer.integrations.slack.tools_channels import slack__invite_to_channel

    result = await slack__invite_to_channel(mock_client, channel="C001", users="U001,U002")
    assert "Invited" in result
    assert "U001" in result


@pytest.mark.asyncio
async def test_kick_from_channel(mock_client):
    from pincer.integrations.slack.tools_channels import slack__kick_from_channel

    result = await slack__kick_from_channel(mock_client, channel="C001", user="U001")
    assert "Removed" in result or "U001" in result


@pytest.mark.asyncio
async def test_open_dm(mock_client):
    from pincer.integrations.slack.tools_channels import slack__open_dm

    result = await slack__open_dm(mock_client, user="U001")
    assert "D123" in result or "DM" in result.lower()


@pytest.mark.asyncio
async def test_open_group_dm(mock_client):
    from pincer.integrations.slack.tools_channels import slack__open_group_dm

    result = await slack__open_group_dm(mock_client, users="U001,U002")
    assert "D123" in result or "Group" in result


@pytest.mark.asyncio
async def test_list_dm_conversations_empty(mock_client):
    from pincer.integrations.slack.tools_channels import slack__list_dm_conversations

    result = await slack__list_dm_conversations(mock_client)
    assert "No DM" in result


@pytest.mark.asyncio
async def test_list_dm_conversations_with_data(mock_client):
    from pincer.integrations.slack.tools_channels import slack__list_dm_conversations

    mock_client.bot.conversations_list.return_value = fake_resp(
        channels=[{"id": "D001", "is_mpim": False, "user": "U123"}],
        response_metadata={"next_cursor": ""},
    )
    result = await slack__list_dm_conversations(mock_client)
    assert "D001" in result


@pytest.mark.asyncio
async def test_list_channels_error_handling(mock_client):
    from pincer.integrations.slack.tools_channels import slack__list_channels

    mock_client.bot.conversations_list.side_effect = Exception("Network error")
    result = await slack__list_channels(mock_client)
    assert "Error" in result


@pytest.mark.asyncio
async def test_register_channel_tools_count():

    from pincer.integrations.slack.tools_channels import register_channel_tools
    from pincer.tools.registry import ToolRegistry

    registry = ToolRegistry()
    client = MagicMock()
    count = register_channel_tools(registry, client)
    assert count == 16
    assert len(registry.list_tools()) == 16


@pytest.mark.asyncio
async def test_channel_write_tools_require_approval():
    """Ensure all channel write tools have require_approval=True."""

    from pincer.integrations.slack.tools_channels import register_channel_tools
    from pincer.tools.registry import ToolRegistry

    registry = ToolRegistry()
    client = MagicMock()
    register_channel_tools(registry, client)

    write_tools = [
        "slack__create_channel",
        "slack__archive_channel",
        "slack__unarchive_channel",
        "slack__rename_channel",
        "slack__set_channel_topic",
        "slack__set_channel_purpose",
        "slack__join_channel",
        "slack__leave_channel",
        "slack__invite_to_channel",
        "slack__kick_from_channel",
    ]
    for name in write_tools:
        assert registry.requires_approval(name), f"{name} should require approval"


@pytest.mark.asyncio
async def test_channel_read_tools_no_approval():
    """Ensure read-only channel tools do NOT require approval."""

    from pincer.integrations.slack.tools_channels import register_channel_tools
    from pincer.tools.registry import ToolRegistry

    registry = ToolRegistry()
    client = MagicMock()
    register_channel_tools(registry, client)

    read_tools = [
        "slack__list_channels",
        "slack__get_channel_info",
        "slack__list_channel_members",
        "slack__open_dm",
        "slack__open_group_dm",
        "slack__list_dm_conversations",
    ]
    for name in read_tools:
        assert not registry.requires_approval(name), f"{name} should NOT require approval"
