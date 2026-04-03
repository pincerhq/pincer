"""
Tests for Gmail tools — one test per tool (19 tools).
"""

from __future__ import annotations

from pincer.integrations.google.tools_gmail import (
    _extract_attachments,
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


async def test_get_attachment_saves_to_disk(mock_factory, mock_gmail_service, tmp_path, monkeypatch):
    """google__get_attachment writes decoded bytes to disk and returns metadata."""
    import base64 as b64mod

    test_content = b"%PDF-1.4 fake pdf content here"
    mock_gmail_service.users().messages().attachments().get().execute.return_value = {
        "data": b64mod.urlsafe_b64encode(test_content).decode(),
        "size": len(test_content),
    }
    mock_gmail_service.users().messages().get().execute.return_value = {
        "id": "msg1",
        "payload": {
            "parts": [{
                "filename": "report.pdf",
                "mimeType": "application/pdf",
                "body": {"attachmentId": "att1", "size": len(test_content)},
            }],
        },
    }

    # Redirect downloads to tmp_path
    download_dir = str(tmp_path / "downloads")
    monkeypatch.setattr("os.path.expanduser", lambda p: p.replace("~/.pincer/downloads", download_dir))

    result = await google__get_attachment(mock_factory, message_id="msg1", attachment_id="att1")

    # Response is short metadata, NOT base64
    assert "report.pdf" in result
    assert "Saved to:" in result
    assert len(result) < 500
    assert "base64" not in result.lower()
    assert "SGVsbG8" not in result  # no raw base64 data

    # File actually exists on disk with correct content
    import os
    save_path = result.split("Saved to: ")[1].split("\n")[0].strip()
    assert os.path.exists(save_path)
    with open(save_path, "rb") as f:
        assert f.read() == test_content


async def test_get_attachment_short_response(mock_factory, mock_gmail_service, tmp_path, monkeypatch):
    """Response must be under 500 chars even for large files — never raw base64."""
    import base64 as b64mod

    big_content = b"x" * 300_000
    mock_gmail_service.users().messages().attachments().get().execute.return_value = {
        "data": b64mod.urlsafe_b64encode(big_content).decode(),
        "size": len(big_content),
    }
    mock_gmail_service.users().messages().get().execute.return_value = {
        "id": "msg1",
        "payload": {"parts": [{
            "filename": "big.bin",
            "body": {"attachmentId": "att1", "size": len(big_content)},
        }]},
    }

    download_dir = str(tmp_path / "downloads")
    monkeypatch.setattr("os.path.expanduser", lambda p: p.replace("~/.pincer/downloads", download_dir))

    result = await google__get_attachment(mock_factory, message_id="msg1", attachment_id="att1")

    assert len(result) < 500
    assert "big.bin" in result
    assert "293" in result or "292" in result  # ~293 KB


async def test_get_attachment_duplicate_filename(mock_factory, mock_gmail_service, tmp_path, monkeypatch):
    """Second download of same filename gets _1 suffix."""
    import base64 as b64mod

    content = b"test"
    mock_gmail_service.users().messages().attachments().get().execute.return_value = {
        "data": b64mod.urlsafe_b64encode(content).decode(),
        "size": 4,
    }
    mock_gmail_service.users().messages().get().execute.return_value = {
        "id": "msg1",
        "payload": {"parts": [{
            "filename": "file.pdf",
            "body": {"attachmentId": "att1"},
        }]},
    }

    download_dir = str(tmp_path / "downloads")
    monkeypatch.setattr("os.path.expanduser", lambda p: p.replace("~/.pincer/downloads", download_dir))

    result1 = await google__get_attachment(mock_factory, message_id="msg1", attachment_id="att1")
    result2 = await google__get_attachment(mock_factory, message_id="msg1", attachment_id="att1")

    path1 = result1.split("Saved to: ")[1].split("\n")[0].strip()
    path2 = result2.split("Saved to: ")[1].split("\n")[0].strip()
    assert path1 != path2
    assert "file_1.pdf" in path2


async def test_get_attachment_empty_data(mock_factory, mock_gmail_service):
    """Empty attachment data returns error, not crash."""
    mock_gmail_service.users().messages().attachments().get().execute.return_value = {
        "data": "", "size": 0,
    }
    result = await google__get_attachment(mock_factory, message_id="msg1", attachment_id="att1")
    assert "error" in result.lower() or "empty" in result.lower()


async def test_get_attachment_with_filename_param(mock_factory, mock_gmail_service, tmp_path, monkeypatch):
    """When filename is passed, no extra messages.get() call is needed."""
    import base64 as b64mod

    content = b"hello"
    mock_gmail_service.users().messages().attachments().get().execute.return_value = {
        "data": b64mod.urlsafe_b64encode(content).decode(),
        "size": len(content),
    }

    download_dir = str(tmp_path / "downloads")
    monkeypatch.setattr("os.path.expanduser", lambda p: p.replace("~/.pincer/downloads", download_dir))

    result = await google__get_attachment(
        mock_factory, message_id="msg1", attachment_id="att1", filename="cv.pdf"
    )

    assert "cv.pdf" in result
    assert "Saved to:" in result
    # messages().get() should NOT have been called since we passed filename
    mock_gmail_service.users().messages().get.assert_not_called()


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


# ── Attachment workflow tests ────────────────────────────────────────────────


def _msg_with_attachment(msg_id="msg123", subject="Contract PDF", from_="sarah@company.com"):
    return {
        "id": msg_id,
        "threadId": "thread1",
        "snippet": "Here is the contract",
        "payload": {
            "headers": _headers(subject, from_),
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": "SGVsbG8gV29ybGQ="},
                },
                {
                    "filename": "contract.pdf",
                    "mimeType": "application/pdf",
                    "body": {"attachmentId": "att_abc123", "size": 245000},
                },
            ],
        },
    }


def test_extract_attachments_with_attachment():
    payload = _msg_with_attachment()["payload"]
    attachments = _extract_attachments(payload)
    assert len(attachments) == 1
    assert attachments[0]["filename"] == "contract.pdf"
    assert attachments[0]["attachment_id"] == "att_abc123"
    assert attachments[0]["mime_type"] == "application/pdf"
    assert attachments[0]["size_bytes"] == 245000


def test_extract_attachments_without_attachment():
    payload = _msg()["payload"]
    attachments = _extract_attachments(payload)
    assert attachments == []


async def test_get_message_includes_attachment_metadata(mock_factory, mock_gmail_service):
    """google__get_message returns attachment IDs and download hint."""
    mock_gmail_service.users().messages().get().execute.return_value = _msg_with_attachment()
    result = await google__get_message(mock_factory, message_id="msg123")
    assert "contract.pdf" in result
    assert "att_abc123" in result
    assert "google__get_attachment" in result
    assert "application/pdf" in result
    assert "245000" in result


async def test_get_message_no_attachments_no_hint(mock_factory, mock_gmail_service):
    """google__get_message without attachments has no download hint."""
    mock_gmail_service.users().messages().get().execute.return_value = _msg()
    result = await google__get_message(mock_factory, message_id="msg1")
    assert "google__get_attachment" not in result
    assert "Attachments:" not in result


# ── Fallback guard tests ────────────────────────────────────────────────────


async def test_fallback_guard_blocks_google_code():
    """_execute_tool blocks Google library imports in python_exec."""
    from pincer.core.agent import _GOOGLE_FALLBACK_PATTERNS

    # Verify patterns exist and contain expected entries
    assert "googleapiclient" in _GOOGLE_FALLBACK_PATTERNS
    assert "gmail" in _GOOGLE_FALLBACK_PATTERNS
    assert "googleapis.com" in _GOOGLE_FALLBACK_PATTERNS


async def test_fallback_guard_pattern_matching():
    """Fallback guard pattern check works correctly."""
    from pincer.core.agent import _GOOGLE_FALLBACK_PATTERNS

    google_code = "from googleapiclient.discovery import build"
    assert any(p in google_code.lower() for p in _GOOGLE_FALLBACK_PATTERNS)

    safe_code = "print(1 + 1)"
    assert not any(p in safe_code.lower() for p in _GOOGLE_FALLBACK_PATTERNS)

    imap_code = "import imaplib; m = imaplib.IMAP4_SSL('imap.gmail.com')"
    assert any(p in imap_code.lower() for p in _GOOGLE_FALLBACK_PATTERNS)
