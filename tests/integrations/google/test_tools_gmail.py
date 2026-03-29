"""
Tests for Gmail tools — one test per tool (19 tools).
"""

from __future__ import annotations

from pincer.integrations.google.tools_gmail import (
    google__add_label,
    google__create_draft,
    google__create_label,
    google__forward_message,
    google__get_attachment,
    google__get_message,
    google__get_thread,
    google__list_labels,
    google__list_messages,
    google__mark_as_read,
    google__mark_as_unread,
    google__remove_label,
    google__reply_all,
    google__reply_to_message,
    google__search_messages,
    google__send_draft,
    google__send_message,
    google__trash_message,
    google__untrash_message,
)


def _headers(subject="Test", from_="alice@example.com", date="Mon, 25 Mar 2026 10:00:00 +0000"):
    return [
        {"name": "Subject", "value": subject},
        {"name": "From", "value": from_},
        {"name": "Date", "value": date},
        {"name": "Message-ID", "value": "<test-msg-id@example.com>"},
    ]


def _msg(msg_id="msg1", subject="Test", from_="alice@example.com", snippet="Hello"):
    return {
        "id": msg_id,
        "threadId": "thread1",
        "snippet": snippet,
        "payload": {
            "headers": _headers(subject, from_),
            "mimeType": "text/plain",
            "body": {"data": "SGVsbG8gV29ybGQ="},
        },
    }


# ── google__list_labels ───────────────────────────────────────────────────────


async def test_list_labels(mock_factory, mock_gmail_service):
    mock_gmail_service.users().labels().list().execute.return_value = {
        "labels": [{"id": "INBOX", "name": "Inbox"}, {"id": "Label_1", "name": "Work"}]
    }
    result = await google__list_labels(mock_factory)
    assert "Inbox" in result
    assert "Work" in result


async def test_list_labels_empty(mock_factory, mock_gmail_service):
    mock_gmail_service.users().labels().list().execute.return_value = {"labels": []}
    result = await google__list_labels(mock_factory)
    assert "No labels" in result


# ── google__list_messages ─────────────────────────────────────────────────────


async def test_list_messages_returns_summaries(mock_factory, mock_gmail_service):
    mock_gmail_service.users().messages().list().execute.return_value = {"messages": [{"id": "msg1"}]}
    mock_gmail_service.users().messages().get().execute.return_value = _msg()
    result = await google__list_messages(mock_factory)
    assert "Test" in result
    assert "alice@example.com" in result


async def test_list_messages_empty(mock_factory, mock_gmail_service):
    mock_gmail_service.users().messages().list().execute.return_value = {"messages": []}
    result = await google__list_messages(mock_factory)
    assert "No messages" in result


# ── google__search_messages ───────────────────────────────────────────────────


async def test_search_messages_found(mock_factory, mock_gmail_service):
    mock_gmail_service.users().messages().list().execute.return_value = {"messages": [{"id": "msg1"}, {"id": "msg2"}]}
    mock_gmail_service.users().messages().get().execute.side_effect = [
        _msg("msg1", "Invoice Q1", "bob@company.com"),
        _msg("msg2", "Invoice Q2", "bob@company.com"),
    ]
    result = await google__search_messages(mock_factory, query="from:bob subject:invoice")
    assert "Invoice Q1" in result
    assert "bob@company.com" in result
    assert "msg1" in result


async def test_search_messages_none_found(mock_factory, mock_gmail_service):
    mock_gmail_service.users().messages().list().execute.return_value = {"messages": []}
    result = await google__search_messages(mock_factory, query="nothing")
    assert "No messages" in result


# ── google__get_message ───────────────────────────────────────────────────────


async def test_get_message_returns_full_content(mock_factory, mock_gmail_service):
    mock_gmail_service.users().messages().get().execute.return_value = _msg()
    result = await google__get_message(mock_factory, message_id="msg1")
    assert "Test" in result
    assert "alice@example.com" in result


# ── google__get_thread ────────────────────────────────────────────────────────


async def test_get_thread_returns_all_messages(mock_factory, mock_gmail_service):
    mock_gmail_service.users().threads().get().execute.return_value = {
        "messages": [_msg("m1", "Re: Thread"), _msg("m2", "Re: Thread")]
    }
    result = await google__get_thread(mock_factory, thread_id="thread1")
    assert "2 message(s)" in result
    assert "Message 1" in result


async def test_get_thread_empty(mock_factory, mock_gmail_service):
    mock_gmail_service.users().threads().get().execute.return_value = {"messages": []}
    result = await google__get_thread(mock_factory, thread_id="thread1")
    assert "empty" in result


# ── google__get_attachment ────────────────────────────────────────────────────


async def test_get_attachment(mock_factory, mock_gmail_service):
    mock_gmail_service.users().messages().attachments().get().execute.return_value = {
        "size": 1234,
        "data": "SGVsbG8gV29ybGQ=",
    }
    result = await google__get_attachment(mock_factory, message_id="msg1", attachment_id="att1")
    assert "1234" in result
    assert "SGVsbG8" in result


# ── google__send_message ──────────────────────────────────────────────────────


async def test_send_message(mock_factory, mock_gmail_service):
    mock_gmail_service.users().messages().send().execute.return_value = {"id": "sent1"}
    result = await google__send_message(mock_factory, to="bob@example.com", subject="Hello", body="World")
    assert "bob@example.com" in result
    assert "Hello" in result


# ── google__reply_to_message ──────────────────────────────────────────────────


async def test_reply_to_message(mock_factory, mock_gmail_service):
    mock_gmail_service.users().messages().get().execute.return_value = _msg()
    mock_gmail_service.users().messages().send().execute.return_value = {"id": "r1"}
    result = await google__reply_to_message(mock_factory, message_id="msg1", body="Thanks!")
    assert "Reply sent" in result


# ── google__reply_all ─────────────────────────────────────────────────────────


async def test_reply_all(mock_factory, mock_gmail_service):
    msg = _msg()
    msg["payload"]["headers"].append({"name": "To", "value": "me@example.com"})
    mock_gmail_service.users().messages().get().execute.return_value = msg
    mock_gmail_service.users().messages().send().execute.return_value = {"id": "r1"}
    result = await google__reply_all(mock_factory, message_id="msg1", body="Noted!")
    assert "Reply-all sent" in result


# ── google__forward_message ───────────────────────────────────────────────────


async def test_forward_message(mock_factory, mock_gmail_service):
    mock_gmail_service.users().messages().get().execute.return_value = _msg()
    mock_gmail_service.users().messages().send().execute.return_value = {"id": "f1"}
    result = await google__forward_message(mock_factory, message_id="msg1", to="carol@example.com")
    assert "carol@example.com" in result
    assert "Fwd:" in result


# ── google__create_draft ──────────────────────────────────────────────────────


async def test_create_draft(mock_factory, mock_gmail_service):
    mock_gmail_service.users().drafts().create().execute.return_value = {"id": "draft1"}
    result = await google__create_draft(mock_factory, to="dave@example.com", subject="Draft Subject", body="Draft body")
    assert "draft1" in result
    assert "Dave" in result or "dave@example.com" in result


# ── google__send_draft ────────────────────────────────────────────────────────


async def test_send_draft(mock_factory, mock_gmail_service):
    mock_gmail_service.users().drafts().send().execute.return_value = {"id": "msg99"}
    result = await google__send_draft(mock_factory, draft_id="draft1")
    assert "draft1" in result
    assert "msg99" in result


# ── google__trash_message ─────────────────────────────────────────────────────


async def test_trash_message(mock_factory, mock_gmail_service):
    mock_gmail_service.users().messages().trash().execute.return_value = {}
    result = await google__trash_message(mock_factory, message_id="msg1")
    assert "Trash" in result


# ── google__untrash_message ───────────────────────────────────────────────────


async def test_untrash_message(mock_factory, mock_gmail_service):
    mock_gmail_service.users().messages().untrash().execute.return_value = {}
    result = await google__untrash_message(mock_factory, message_id="msg1")
    assert "restored" in result.lower() or "Trash" in result


# ── google__mark_as_read ──────────────────────────────────────────────────────


async def test_mark_as_read(mock_factory, mock_gmail_service):
    mock_gmail_service.users().messages().modify().execute.return_value = {}
    result = await google__mark_as_read(mock_factory, message_id="msg1")
    assert "read" in result.lower()


# ── google__mark_as_unread ────────────────────────────────────────────────────


async def test_mark_as_unread(mock_factory, mock_gmail_service):
    mock_gmail_service.users().messages().modify().execute.return_value = {}
    result = await google__mark_as_unread(mock_factory, message_id="msg1")
    assert "unread" in result.lower()


# ── google__add_label ─────────────────────────────────────────────────────────


async def test_add_label(mock_factory, mock_gmail_service):
    mock_gmail_service.users().messages().modify().execute.return_value = {}
    result = await google__add_label(mock_factory, message_id="msg1", label_id="Label_1")
    assert "Label_1" in result


# ── google__remove_label ──────────────────────────────────────────────────────


async def test_remove_label(mock_factory, mock_gmail_service):
    mock_gmail_service.users().messages().modify().execute.return_value = {}
    result = await google__remove_label(mock_factory, message_id="msg1", label_id="Label_1")
    assert "Label_1" in result


# ── google__create_label ──────────────────────────────────────────────────────


async def test_create_label(mock_factory, mock_gmail_service):
    mock_gmail_service.users().labels().create().execute.return_value = {"id": "Label_2", "name": "ProjectX"}
    result = await google__create_label(mock_factory, name="ProjectX")
    assert "ProjectX" in result
    assert "Label_2" in result
