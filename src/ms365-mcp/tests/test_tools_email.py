"""Tests for MS365 email tools — one test per tool."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from ms365.tools_email import (
    outlook__create_draft,
    outlook__delete_message,
    outlook__download_attachment,
    outlook__flag_message,
    outlook__forward_message,
    outlook__get_message,
    outlook__get_message_attachments,
    outlook__list_mail_folders,
    outlook__list_messages,
    outlook__mark_as_read,
    outlook__move_message,
    outlook__reply_all_to_message,
    outlook__reply_to_message,
    outlook__search_messages,
    outlook__send_draft,
    outlook__send_message,
    outlook__update_draft,
)

if TYPE_CHECKING:
    from unittest.mock import MagicMock


@pytest.mark.asyncio
async def test_list_mail_folders(mock_client: MagicMock) -> None:
    mock_client.get.return_value = {
        "value": [
            {"displayName": "Inbox", "totalItemCount": 42, "unreadItemCount": 5, "id": "inbox-id"},
            {"displayName": "Sent Items", "totalItemCount": 10, "unreadItemCount": 0, "id": "sent-id"},
        ]
    }
    result = await outlook__list_mail_folders(mock_client)
    assert "Inbox" in result
    assert "42 messages" in result
    assert "5 unread" in result
    assert "2 mail folder(s)" in result


@pytest.mark.asyncio
async def test_list_messages(mock_client: MagicMock) -> None:
    mock_client.get.return_value = {
        "value": [
            {
                "id": "msg-1",
                "subject": "Q3 Report",
                "from": {"emailAddress": {"name": "Sarah", "address": "sarah@example.com"}},
                "receivedDateTime": "2026-03-26T10:00:00Z",
                "isRead": False,
                "hasAttachments": True,
                "bodyPreview": "Please find the Q3 report attached.",
            }
        ]
    }
    result = await outlook__list_messages(mock_client)
    assert "Q3 Report" in result
    assert "sarah@example.com" in result
    assert "Unread" in result


@pytest.mark.asyncio
async def test_list_messages_unread_filter(mock_client: MagicMock) -> None:
    mock_client.get.return_value = {"value": []}
    await outlook__list_messages(mock_client, unread_only=True)
    call_args = mock_client.get.call_args
    params = call_args[1].get("params") or (call_args[0][1] if len(call_args[0]) > 1 else {})
    assert "isRead eq false" in str(params)


@pytest.mark.asyncio
async def test_search_messages(mock_client: MagicMock) -> None:
    mock_client.get.return_value = {
        "value": [
            {
                "id": "msg-search-1",
                "subject": "Budget Meeting",
                "from": {"emailAddress": {"name": "Bob", "address": "bob@example.com"}},
                "receivedDateTime": "2026-03-25T14:00:00Z",
                "isRead": True,
                "bodyPreview": "Let's discuss the budget.",
            }
        ]
    }
    result = await outlook__search_messages(mock_client, query="budget")
    assert "Budget Meeting" in result
    assert "bob@example.com" in result


@pytest.mark.asyncio
async def test_get_message(mock_client: MagicMock) -> None:
    mock_client.get.return_value = {
        "id": "msg-full",
        "subject": "Important Update",
        "from": {"emailAddress": {"name": "Alice", "address": "alice@example.com"}},
        "toRecipients": [{"emailAddress": {"address": "me@example.com"}}],
        "ccRecipients": [],
        "bccRecipients": [],
        "receivedDateTime": "2026-03-26T09:00:00Z",
        "body": {"contentType": "text", "content": "This is the message body."},
        "isRead": True,
        "hasAttachments": False,
        "importance": "high",
        "flag": {"flagStatus": "notFlagged"},
    }
    result = await outlook__get_message(mock_client, message_id="msg-full")
    assert "Important Update" in result
    assert "alice@example.com" in result
    assert "This is the message body" in result


@pytest.mark.asyncio
async def test_get_attachments(mock_client: MagicMock) -> None:
    mock_client.get.return_value = {
        "value": [
            {"id": "att-1", "name": "report.pdf", "contentType": "application/pdf", "size": 102400},
        ]
    }
    result = await outlook__get_message_attachments(mock_client, message_id="msg-1")
    assert "report.pdf" in result
    assert "100.0 KB" in result


@pytest.mark.asyncio
async def test_download_attachment(mock_client: MagicMock) -> None:
    mock_client.get.return_value = {
        "id": "att-1",
        "name": "notes.txt",
        "contentType": "application/octet-stream",
        "size": 500,
        "contentBytes": "",
    }
    result = await outlook__download_attachment(mock_client, message_id="msg-1", attachment_id="att-1")
    assert "notes.txt" in result


@pytest.mark.asyncio
async def test_send_message(mock_client: MagicMock) -> None:
    mock_client.post.return_value = {}
    result = await outlook__send_message(mock_client, to="test@example.com", subject="Hello", body="Hi there!")
    assert "Email sent to test@example.com" in result
    assert '"Hello"' in result
    mock_client.post.assert_called_once()
    call_json = mock_client.post.call_args[1]["json"]
    assert call_json["message"]["subject"] == "Hello"


@pytest.mark.asyncio
async def test_send_message_with_cc_bcc(mock_client: MagicMock) -> None:
    mock_client.post.return_value = {}
    result = await outlook__send_message(
        mock_client, to="a@example.com", subject="Test", body="Body", cc="b@example.com", bcc="c@example.com"
    )
    assert "Email sent" in result
    call_json = mock_client.post.call_args[1]["json"]
    assert len(call_json["message"]["ccRecipients"]) == 1
    assert len(call_json["message"]["bccRecipients"]) == 1


@pytest.mark.asyncio
async def test_reply_to_message(mock_client: MagicMock) -> None:
    mock_client.post.return_value = {}
    result = await outlook__reply_to_message(mock_client, message_id="msg-1", body="Thanks!")
    assert "Reply sent" in result


@pytest.mark.asyncio
async def test_reply_all(mock_client: MagicMock) -> None:
    mock_client.post.return_value = {}
    result = await outlook__reply_all_to_message(mock_client, message_id="msg-1", body="Noted.")
    assert "Reply-all sent" in result


@pytest.mark.asyncio
async def test_forward_message(mock_client: MagicMock) -> None:
    mock_client.post.return_value = {}
    result = await outlook__forward_message(mock_client, message_id="msg-1", to="bob@example.com")
    assert "forwarded" in result
    assert "bob@example.com" in result


@pytest.mark.asyncio
async def test_create_draft(mock_client: MagicMock) -> None:
    mock_client.post.return_value = {"id": "draft-1"}
    result = await outlook__create_draft(mock_client, to="a@example.com", subject="Draft", body="Content")
    assert "Draft created" in result
    assert "draft-1" in result


@pytest.mark.asyncio
async def test_update_draft(mock_client: MagicMock) -> None:
    mock_client.patch.return_value = {}
    result = await outlook__update_draft(mock_client, message_id="draft-1", subject="Updated Subject")
    assert "updated" in result


@pytest.mark.asyncio
async def test_send_draft(mock_client: MagicMock) -> None:
    mock_client.post.return_value = {}
    result = await outlook__send_draft(mock_client, message_id="draft-1")
    assert "sent" in result


@pytest.mark.asyncio
async def test_move_message(mock_client: MagicMock) -> None:
    mock_client.post.return_value = {"id": "msg-moved"}
    result = await outlook__move_message(mock_client, message_id="msg-1", destination_folder="Archive")
    assert "moved" in result
    assert "Archive" in result


@pytest.mark.asyncio
async def test_delete_message(mock_client: MagicMock) -> None:
    mock_client.delete.return_value = {}
    result = await outlook__delete_message(mock_client, message_id="msg-1")
    assert "deleted" in result


@pytest.mark.asyncio
async def test_mark_as_read(mock_client: MagicMock) -> None:
    mock_client.patch.return_value = {}
    result = await outlook__mark_as_read(mock_client, message_id="msg-1")
    assert "read" in result
    call_json = mock_client.patch.call_args[1]["json"]
    assert call_json["isRead"] is True


@pytest.mark.asyncio
async def test_flag_message(mock_client: MagicMock) -> None:
    mock_client.patch.return_value = {}
    result = await outlook__flag_message(mock_client, message_id="msg-1", flagged=True)
    assert "flagged" in result
