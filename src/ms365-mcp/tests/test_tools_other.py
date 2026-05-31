"""Tests for MS365 To Do, Teams, Contacts, and OneNote tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from ms365.tools_contacts import (
    outlook__create_contact,
    outlook__delete_contact,
    outlook__get_contact,
    outlook__list_contacts,
    outlook__search_contacts,
    outlook__update_contact,
)
from ms365.tools_onenote import (
    onenote__create_page,
    onenote__get_page_content,
    onenote__list_notebooks,
    onenote__list_pages,
    onenote__list_sections,
)
from ms365.tools_teams import (
    teams__get_channel_message,
    teams__list_channel_messages,
    teams__list_channels,
    teams__list_chats,
    teams__list_teams,
    teams__send_channel_message,
    teams__send_chat_message,
)
from ms365.tools_todo import (
    ms_todo__complete_task,
    ms_todo__create_task,
    ms_todo__create_task_list,
    ms_todo__delete_task,
    ms_todo__get_task,
    ms_todo__list_task_lists,
    ms_todo__list_tasks,
    ms_todo__update_task,
)

if TYPE_CHECKING:
    from unittest.mock import MagicMock

# ── To Do ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_task_lists(mock_client: MagicMock) -> None:
    mock_client.get.return_value = {"value": [{"id": "list-1", "displayName": "Tasks"}]}
    result = await ms_todo__list_task_lists(mock_client)
    assert "Tasks" in result


@pytest.mark.asyncio
async def test_list_tasks(mock_client: MagicMock) -> None:
    mock_client.get.return_value = {
        "value": [
            {
                "id": "task-1",
                "title": "Buy groceries",
                "status": "notStarted",
                "importance": "normal",
                "dueDateTime": {"dateTime": "2026-03-28T00:00:00"},
            }
        ]
    }
    result = await ms_todo__list_tasks(mock_client, list_id="list-1")
    assert "Buy groceries" in result


@pytest.mark.asyncio
async def test_get_task(mock_client: MagicMock) -> None:
    mock_client.get.return_value = {
        "id": "task-1",
        "title": "Review PR",
        "status": "inProgress",
        "importance": "high",
        "dueDateTime": {"dateTime": "2026-03-27T00:00:00"},
        "body": {"content": "Check the tests"},
        "createdDateTime": "2026-03-25T10:00:00Z",
        "lastModifiedDateTime": "2026-03-26T10:00:00Z",
    }
    result = await ms_todo__get_task(mock_client, list_id="list-1", task_id="task-1")
    assert "Review PR" in result
    assert "inProgress" in result


@pytest.mark.asyncio
async def test_create_task(mock_client: MagicMock) -> None:
    mock_client.post.return_value = {"id": "task-new"}
    result = await ms_todo__create_task(mock_client, list_id="list-1", title="New task")
    assert "Task created" in result
    assert "New task" in result


@pytest.mark.asyncio
async def test_update_task(mock_client: MagicMock) -> None:
    mock_client.patch.return_value = {}
    result = await ms_todo__update_task(mock_client, list_id="list-1", task_id="task-1", title="Updated")
    assert "updated" in result


@pytest.mark.asyncio
async def test_complete_task(mock_client: MagicMock) -> None:
    mock_client.patch.return_value = {}
    result = await ms_todo__complete_task(mock_client, list_id="list-1", task_id="task-1")
    assert "completed" in result


@pytest.mark.asyncio
async def test_delete_task(mock_client: MagicMock) -> None:
    mock_client.delete.return_value = {}
    result = await ms_todo__delete_task(mock_client, list_id="list-1", task_id="task-1")
    assert "deleted" in result


@pytest.mark.asyncio
async def test_create_task_list(mock_client: MagicMock) -> None:
    mock_client.post.return_value = {"id": "list-new"}
    result = await ms_todo__create_task_list(mock_client, name="Shopping")
    assert "Task list created" in result
    assert "Shopping" in result


# ── Teams ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_teams(mock_client: MagicMock) -> None:
    mock_client.get.return_value = {
        "value": [{"id": "team-1", "displayName": "Engineering", "description": "Dev team"}]
    }
    result = await teams__list_teams(mock_client)
    assert "Engineering" in result


@pytest.mark.asyncio
async def test_list_channels(mock_client: MagicMock) -> None:
    mock_client.get.return_value = {"value": [{"id": "ch-1", "displayName": "General", "membershipType": "standard"}]}
    result = await teams__list_channels(mock_client, team_id="team-1")
    assert "General" in result


@pytest.mark.asyncio
async def test_list_channel_messages(mock_client: MagicMock) -> None:
    mock_client.get.return_value = {
        "value": [
            {
                "id": "msg-1",
                "createdDateTime": "2026-03-26T10:00:00Z",
                "from": {"user": {"displayName": "Alice"}},
                "body": {"content": "Hello everyone!"},
            }
        ]
    }
    result = await teams__list_channel_messages(mock_client, team_id="team-1", channel_id="ch-1")
    assert "Alice" in result
    assert "Hello everyone" in result


@pytest.mark.asyncio
async def test_get_channel_message(mock_client: MagicMock) -> None:
    mock_client.get.return_value = {
        "id": "msg-1",
        "createdDateTime": "2026-03-26T10:00:00Z",
        "from": {"user": {"displayName": "Bob"}},
        "body": {"content": "Meeting notes here"},
        "messageType": "message",
        "attachments": [],
    }
    result = await teams__get_channel_message(mock_client, team_id="t1", channel_id="ch1", message_id="msg-1")
    assert "Bob" in result
    assert "Meeting notes" in result


@pytest.mark.asyncio
async def test_send_channel_message(mock_client: MagicMock) -> None:
    mock_client.post.return_value = {"id": "msg-new"}
    result = await teams__send_channel_message(mock_client, team_id="t1", channel_id="ch1", body="Test message")
    assert "Message sent" in result


@pytest.mark.asyncio
async def test_list_chats(mock_client: MagicMock) -> None:
    mock_client.get.return_value = {
        "value": [{"id": "chat-1", "chatType": "oneOnOne", "topic": "", "members": [{"displayName": "Carol"}]}]
    }
    result = await teams__list_chats(mock_client)
    assert "oneOnOne" in result


@pytest.mark.asyncio
async def test_send_chat_message(mock_client: MagicMock) -> None:
    mock_client.post.return_value = {"id": "chat-msg-1"}
    result = await teams__send_chat_message(mock_client, chat_id="chat-1", body="Hi!")
    assert "Chat message sent" in result


# ── Contacts ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_contacts(mock_client: MagicMock) -> None:
    mock_client.get.return_value = {
        "value": [
            {
                "id": "ct-1",
                "displayName": "Jane Doe",
                "emailAddresses": [{"address": "jane@example.com"}],
                "businessPhones": ["555-1234"],
                "mobilePhone": "",
                "companyName": "Acme",
                "jobTitle": "CTO",
            }
        ]
    }
    result = await outlook__list_contacts(mock_client)
    assert "Jane Doe" in result
    assert "jane@example.com" in result


@pytest.mark.asyncio
async def test_search_contacts(mock_client: MagicMock) -> None:
    mock_client.get.return_value = {
        "value": [
            {
                "id": "ct-2",
                "displayName": "John Smith",
                "emailAddresses": [{"address": "john@example.com"}],
                "businessPhones": [],
                "mobilePhone": "",
                "companyName": "",
                "jobTitle": "",
            }
        ]
    }
    result = await outlook__search_contacts(mock_client, query="John")
    assert "John Smith" in result


@pytest.mark.asyncio
async def test_get_contact(mock_client: MagicMock) -> None:
    mock_client.get.return_value = {
        "id": "ct-1",
        "displayName": "Jane Doe",
        "givenName": "Jane",
        "surname": "Doe",
        "emailAddresses": [{"address": "jane@example.com"}],
        "businessPhones": ["555-1234"],
        "mobilePhone": "555-5678",
        "companyName": "Acme",
        "jobTitle": "CTO",
        "department": "Engineering",
        "businessAddress": {},
    }
    result = await outlook__get_contact(mock_client, contact_id="ct-1")
    assert "Jane Doe" in result
    assert "555-5678" in result


@pytest.mark.asyncio
async def test_create_contact(mock_client: MagicMock) -> None:
    mock_client.post.return_value = {"id": "ct-new", "displayName": "New Person"}
    result = await outlook__create_contact(mock_client, given_name="New", surname="Person", email="new@example.com")
    assert "Contact created" in result
    assert "New Person" in result


@pytest.mark.asyncio
async def test_update_contact(mock_client: MagicMock) -> None:
    mock_client.patch.return_value = {}
    result = await outlook__update_contact(mock_client, contact_id="ct-1", job_title="CEO")
    assert "updated" in result


@pytest.mark.asyncio
async def test_delete_contact(mock_client: MagicMock) -> None:
    mock_client.delete.return_value = {}
    result = await outlook__delete_contact(mock_client, contact_id="ct-1")
    assert "deleted" in result


# ── OneNote ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_notebooks(mock_client: MagicMock) -> None:
    mock_client.get.return_value = {
        "value": [{"id": "nb-1", "displayName": "Work Notes", "lastModifiedDateTime": "2026-03-26T10:00:00Z"}]
    }
    result = await onenote__list_notebooks(mock_client)
    assert "Work Notes" in result


@pytest.mark.asyncio
async def test_list_sections(mock_client: MagicMock) -> None:
    mock_client.get.return_value = {
        "value": [{"id": "sec-1", "displayName": "Meeting Notes", "lastModifiedDateTime": "2026-03-26T10:00:00Z"}]
    }
    result = await onenote__list_sections(mock_client, notebook_id="nb-1")
    assert "Meeting Notes" in result


@pytest.mark.asyncio
async def test_list_pages(mock_client: MagicMock) -> None:
    mock_client.get.return_value = {
        "value": [{"id": "page-1", "title": "Sprint Retro", "lastModifiedDateTime": "2026-03-26T10:00:00Z"}]
    }
    result = await onenote__list_pages(mock_client, section_id="sec-1")
    assert "Sprint Retro" in result


@pytest.mark.asyncio
async def test_get_page_content(mock_client: MagicMock) -> None:
    mock_client.get_binary.return_value = b"<html><body><p>Page content here</p></body></html>"
    result = await onenote__get_page_content(mock_client, page_id="page-1")
    assert "Page content here" in result


@pytest.mark.asyncio
async def test_create_page(mock_client: MagicMock) -> None:
    mock_client.put.return_value = {"id": "page-new"}
    result = await onenote__create_page(mock_client, section_id="sec-1", title="New Page", content="Hello")
    assert "Page created" in result
    assert "New Page" in result
