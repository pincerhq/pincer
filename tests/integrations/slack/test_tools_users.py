"""Tests for Slack user tools (10 tools)."""

from __future__ import annotations

import pytest


def fake_resp(**kwargs):
    d = {"ok": True}
    d.update(kwargs)
    return d


@pytest.mark.asyncio
async def test_list_users_empty(mock_client):
    from pincer.integrations.slack.tools_users import slack__list_users

    result = await slack__list_users(mock_client)
    assert "No users" in result


@pytest.mark.asyncio
async def test_list_users_with_data(mock_client):
    from pincer.integrations.slack.tools_users import slack__list_users

    mock_client.bot.users_list.return_value = fake_resp(
        members=[
            {
                "id": "U001",
                "name": "alice",
                "real_name": "Alice Smith",
                "is_bot": False,
                "deleted": False,
                "profile": {"email": "alice@example.com"},
            },
        ],
        response_metadata={"next_cursor": ""},
    )
    result = await slack__list_users(mock_client)
    assert "Alice Smith" in result


@pytest.mark.asyncio
async def test_list_users_excludes_bots_by_default(mock_client):
    from pincer.integrations.slack.tools_users import slack__list_users

    mock_client.bot.users_list.return_value = fake_resp(
        members=[
            {"id": "U001", "name": "alice", "real_name": "Alice", "is_bot": False, "deleted": False, "profile": {}},
            {
                "id": "B001",
                "name": "slackbot",
                "real_name": "Slackbot",
                "is_bot": True,
                "deleted": False,
                "profile": {},
            },
        ],
        response_metadata={"next_cursor": ""},
    )
    result = await slack__list_users(mock_client, include_bots=False)
    assert "Alice" in result
    assert "Slackbot" not in result


@pytest.mark.asyncio
async def test_get_user_info(mock_client):
    from pincer.integrations.slack.tools_users import slack__get_user_info

    mock_client.bot.users_info.return_value = fake_resp(
        user={
            "id": "U001",
            "name": "alice",
            "real_name": "Alice Smith",
            "is_bot": False,
            "is_admin": False,
            "tz": "America/New_York",
            "tz_label": "EST",
            "profile": {
                "email": "alice@example.com",
                "title": "Engineer",
                "phone": "+1234567890",
                "status_emoji": ":coffee:",
                "status_text": "Coding",
            },
        }
    )
    result = await slack__get_user_info(mock_client, user="U001")
    assert "Alice Smith" in result
    assert "alice@example.com" in result
    assert "Engineer" in result


@pytest.mark.asyncio
async def test_get_user_by_email(mock_client):
    from pincer.integrations.slack.tools_users import slack__get_user_by_email

    result = await slack__get_user_by_email(mock_client, email="alice@example.com")
    assert "U123" in result or "alice" in result


@pytest.mark.asyncio
async def test_set_user_status(mock_client):
    from pincer.integrations.slack.tools_users import slack__set_user_status

    result = await slack__set_user_status(mock_client, status_text="Working from home", status_emoji=":house:")
    assert "Working from home" in result


@pytest.mark.asyncio
async def test_get_user_presence(mock_client):
    from pincer.integrations.slack.tools_users import slack__get_user_presence

    result = await slack__get_user_presence(mock_client, user="U001")
    assert "active" in result


@pytest.mark.asyncio
async def test_list_user_groups_empty(mock_client):
    from pincer.integrations.slack.tools_users import slack__list_user_groups

    result = await slack__list_user_groups(mock_client)
    assert "No user groups" in result


@pytest.mark.asyncio
async def test_list_user_groups_with_data(mock_client):
    from pincer.integrations.slack.tools_users import slack__list_user_groups

    mock_client.bot.usergroups_list.return_value = fake_resp(
        usergroups=[
            {"id": "S001", "name": "Engineering", "handle": "eng", "user_count": 5, "date_delete": None},
        ]
    )
    result = await slack__list_user_groups(mock_client)
    assert "Engineering" in result
    assert "@eng" in result


@pytest.mark.asyncio
async def test_get_user_group_members(mock_client):
    from pincer.integrations.slack.tools_users import slack__get_user_group_members

    mock_client.bot.usergroups_users_list.return_value = fake_resp(users=["U001", "U002"])
    result = await slack__get_user_group_members(mock_client, usergroup="S001")
    assert "U001" in result
    assert "2" in result


@pytest.mark.asyncio
async def test_create_user_group(mock_client):
    from pincer.integrations.slack.tools_users import slack__create_user_group

    result = await slack__create_user_group(mock_client, name="Engineering", handle="eng")
    assert "Engineering" in result or "S123" in result


@pytest.mark.asyncio
async def test_update_user_group(mock_client):
    from pincer.integrations.slack.tools_users import slack__update_user_group

    result = await slack__update_user_group(mock_client, usergroup="S001", name="Platform Eng")
    assert "updated" in result.lower() or "Platform" in result


@pytest.mark.asyncio
async def test_disable_user_group(mock_client):
    from pincer.integrations.slack.tools_users import slack__disable_user_group

    result = await slack__disable_user_group(mock_client, usergroup="S001")
    assert "disabled" in result.lower()


@pytest.mark.asyncio
async def test_register_user_tools_count():
    from unittest.mock import MagicMock

    from pincer.integrations.slack.tools_users import register_user_tools
    from pincer.tools.registry import ToolRegistry

    registry = ToolRegistry()
    client = MagicMock()
    count = register_user_tools(registry, client)
    assert count == 10
    assert len(registry.list_tools()) == 10


@pytest.mark.asyncio
async def test_user_write_tools_require_approval():
    from unittest.mock import MagicMock

    from pincer.integrations.slack.tools_users import register_user_tools
    from pincer.tools.registry import ToolRegistry

    registry = ToolRegistry()
    client = MagicMock()
    register_user_tools(registry, client)

    write_tools = [
        "slack__set_user_status",
        "slack__create_user_group",
        "slack__update_user_group",
        "slack__disable_user_group",
    ]
    for name in write_tools:
        assert registry.requires_approval(name), f"{name} should require approval"
