"""Shared fixtures for Slack integration tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


def fake_resp(**kwargs: Any) -> dict:
    """Build a fake Slack API response dict."""
    return {"ok": True, **kwargs}


@pytest.fixture
def mock_bot() -> MagicMock:
    """A MagicMock representing AsyncWebClient for bot operations."""
    bot = MagicMock()
    # Default: most calls return ok=True
    bot.conversations_list = AsyncMock(return_value=fake_resp(channels=[], response_metadata={"next_cursor": ""}))
    bot.conversations_info = AsyncMock(return_value=fake_resp(channel={}))
    bot.conversations_members = AsyncMock(return_value=fake_resp(members=[], response_metadata={"next_cursor": ""}))
    bot.conversations_create = AsyncMock(return_value=fake_resp(channel={"id": "C123", "name": "test"}))
    bot.conversations_archive = AsyncMock(return_value=fake_resp())
    bot.conversations_unarchive = AsyncMock(return_value=fake_resp())
    bot.conversations_rename = AsyncMock(return_value=fake_resp(channel={"id": "C123", "name": "new-name"}))
    bot.conversations_setTopic = AsyncMock(return_value=fake_resp())
    bot.conversations_setPurpose = AsyncMock(return_value=fake_resp())
    bot.conversations_join = AsyncMock(return_value=fake_resp(channel={"name": "general"}))
    bot.conversations_leave = AsyncMock(return_value=fake_resp())
    bot.conversations_invite = AsyncMock(return_value=fake_resp())
    bot.conversations_kick = AsyncMock(return_value=fake_resp())
    bot.conversations_open = AsyncMock(return_value=fake_resp(channel={"id": "D123"}))
    bot.conversations_history = AsyncMock(return_value=fake_resp(messages=[], has_more=False))
    bot.conversations_replies = AsyncMock(return_value=fake_resp(messages=[]))
    bot.conversations_mark = AsyncMock(return_value=fake_resp())
    bot.chat_getPermalink = AsyncMock(return_value=fake_resp(permalink="https://slack.com/p/123"))
    bot.chat_postMessage = AsyncMock(return_value=fake_resp(ts="123.456"))
    bot.chat_postEphemeral = AsyncMock(return_value=fake_resp(message_ts="123.456"))
    bot.chat_update = AsyncMock(return_value=fake_resp(ts="123.456"))
    bot.chat_delete = AsyncMock(return_value=fake_resp(ts="123.456"))
    bot.chat_scheduleMessage = AsyncMock(return_value=fake_resp(scheduled_message_id="Qxxx"))
    bot.chat_scheduledMessages_list = AsyncMock(return_value=fake_resp(scheduled_messages=[]))
    bot.chat_deleteScheduledMessage = AsyncMock(return_value=fake_resp())
    bot.files_list = AsyncMock(return_value=fake_resp(files=[]))
    bot.files_info = AsyncMock(return_value=fake_resp(file={}))
    bot.files_upload_v2 = AsyncMock(return_value=fake_resp(file={"id": "F123", "name": "test.txt"}))
    bot.files_upload = AsyncMock(return_value=fake_resp(file={"id": "F123", "name": "test.txt"}))
    bot.files_delete = AsyncMock(return_value=fake_resp())
    bot.files_remote_list = AsyncMock(return_value=fake_resp(files=[]))
    bot.files_remote_add = AsyncMock(return_value=fake_resp(file={"id": "F123", "title": "Test"}))
    bot.files_remote_update = AsyncMock(return_value=fake_resp(file={"id": "F123", "title": "Test"}))
    bot.files_remote_remove = AsyncMock(return_value=fake_resp())
    bot.users_list = AsyncMock(return_value=fake_resp(members=[], response_metadata={"next_cursor": ""}))
    bot.users_info = AsyncMock(return_value=fake_resp(user={"id": "U123", "name": "alice", "profile": {}}))
    bot.users_lookupByEmail = AsyncMock(return_value=fake_resp(user={"id": "U123", "name": "alice", "profile": {}}))
    bot.users_getPresence = AsyncMock(return_value=fake_resp(presence="active", online=True))
    bot.users_profile_set = AsyncMock(return_value=fake_resp())
    bot.usergroups_list = AsyncMock(return_value=fake_resp(usergroups=[]))
    bot.usergroups_users_list = AsyncMock(return_value=fake_resp(users=[]))
    bot.usergroups_create = AsyncMock(return_value=fake_resp(usergroup={"id": "S123", "name": "eng", "handle": "eng"}))
    bot.usergroups_update = AsyncMock(return_value=fake_resp(usergroup={"id": "S123", "name": "eng"}))
    bot.usergroups_users_update = AsyncMock(return_value=fake_resp())
    bot.usergroups_disable = AsyncMock(return_value=fake_resp(usergroup={"id": "S123", "name": "eng"}))
    bot.reactions_add = AsyncMock(return_value=fake_resp())
    bot.reactions_remove = AsyncMock(return_value=fake_resp())
    bot.reactions_get = AsyncMock(return_value=fake_resp(message={"reactions": []}))
    bot.reactions_list = AsyncMock(return_value=fake_resp(items=[]))
    bot.emoji_list = AsyncMock(return_value=fake_resp(emoji={}))
    bot.pins_add = AsyncMock(return_value=fake_resp())
    bot.pins_remove = AsyncMock(return_value=fake_resp())
    bot.pins_list = AsyncMock(return_value=fake_resp(items=[]))
    bot.bookmarks_add = AsyncMock(return_value=fake_resp(bookmark={"id": "Bk123", "title": "Test"}))
    bot.bookmarks_list = AsyncMock(return_value=fake_resp(bookmarks=[]))
    bot.bookmarks_remove = AsyncMock(return_value=fake_resp())
    bot.reminders_add = AsyncMock(return_value=fake_resp(reminder={"id": "Rm123", "text": "test", "time": 9999}))
    bot.reminders_list = AsyncMock(return_value=fake_resp(reminders=[]))
    bot.reminders_complete = AsyncMock(return_value=fake_resp())
    bot.reminders_delete = AsyncMock(return_value=fake_resp())
    bot.search_messages = AsyncMock(return_value=fake_resp(messages={"matches": [], "total": 0}))
    bot.search_files = AsyncMock(return_value=fake_resp(files={"matches": [], "total": 0}))
    bot.auth_test = AsyncMock(return_value=fake_resp(team="TestWS", user="bot", team_id="T123", user_id="U1"))
    # Expose token attribute for download_file
    bot.token = "xoxb-test-token"
    return bot


@pytest.fixture
def mock_client(mock_bot: MagicMock) -> MagicMock:
    """A MagicMock SlackClient with bot and user pointing to mock_bot."""
    client = MagicMock()
    client.bot = mock_bot
    client.user = mock_bot  # user falls back to bot in tests
    client.has_user_token = False
    return client
