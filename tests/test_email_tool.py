"""Tests for email tool."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_imap_client(**overrides):
    """Create a mock IMAP client with sensible defaults."""
    client = AsyncMock()
    client.wait_hello_from_server = AsyncMock()
    client.login = AsyncMock()
    client.select = AsyncMock(return_value=("OK", [b"1"]))
    client.logout = AsyncMock()
    client.uid = AsyncMock(return_value=("OK", [b""]))
    client.uid_search = AsyncMock(return_value=("OK", [b""]))
    client.expunge = AsyncMock(return_value=("OK", []))
    client.list = AsyncMock(return_value=("OK", []))
    for key, val in overrides.items():
        setattr(client, key, val)
    return client


# ── email_check ──────────────────────────────────


@pytest.mark.asyncio
class TestEmailCheck:
    async def test_no_unread(self):
        mock_client = _make_imap_client()
        mock_client.uid_search = AsyncMock(return_value=("OK", [b""]))

        with patch("pincer.tools.builtin.email_tool._get_imap_client", return_value=mock_client):
            from pincer.tools.builtin.email_tool import email_check
            result = await email_check()
            assert "No unread" in result

    async def test_returns_uids(self):
        mock_client = _make_imap_client()

        header_bytes = (
            b"From: alice@example.com\r\n"
            b"Subject: Hello\r\n"
            b"Date: Thu, 27 Feb 2026 10:00:00 +0000\r\n"
        )

        mock_client.uid_search = AsyncMock(return_value=("OK", [b"100 200"]))

        async def mock_uid(command, *args):
            if command == "fetch":
                return ("OK", [b"1 FETCH (BODY[HEADER] {0})", bytearray(header_bytes), b")"])
            return ("OK", [b""])

        mock_client.uid = mock_uid

        with patch("pincer.tools.builtin.email_tool._get_imap_client", return_value=mock_client):
            from pincer.tools.builtin.email_tool import email_check
            result = await email_check()
            assert "UID: 200" in result or "UID: 100" in result
            assert "alice@example.com" in result

    async def test_check_all_status(self):
        mock_client = _make_imap_client()

        header_bytes = (
            b"From: spam@example.com\r\n"
            b"Subject: Buy now\r\n"
            b"Date: Thu, 27 Feb 2026 10:00:00 +0000\r\n"
        )

        mock_client.uid_search = AsyncMock(return_value=("OK", [b"10 20 30"]))

        async def mock_uid(command, *args):
            if command == "fetch":
                return ("OK", [b"1 FETCH (BODY[HEADER] {0})", bytearray(header_bytes), b")"])
            return ("OK", [b""])

        mock_client.uid = mock_uid

        with patch("pincer.tools.builtin.email_tool._get_imap_client", return_value=mock_client):
            from pincer.tools.builtin.email_tool import email_check
            result = await email_check(folder="[Gmail]/Spam", status="ALL")
            mock_client.uid_search.assert_called_once_with("ALL")
            mock_client.select.assert_called_once_with('"[Gmail]/Spam"')
            assert "Total: 3 email(s)" in result
            assert "[Gmail]/Spam" in result

    async def test_check_all_no_emails(self):
        mock_client = _make_imap_client()
        mock_client.uid_search = AsyncMock(return_value=("OK", [b""]))

        with patch("pincer.tools.builtin.email_tool._get_imap_client", return_value=mock_client):
            from pincer.tools.builtin.email_tool import email_check
            result = await email_check(folder="[Gmail]/Trash", status="ALL")
            assert "No emails" in result
            assert "ALL" in result

    async def test_error_handling(self):
        with patch(
            "pincer.tools.builtin.email_tool._get_imap_client",
            side_effect=Exception("Connection failed"),
        ):
            from pincer.tools.builtin.email_tool import email_check
            result = await email_check()
            assert "Error" in result


# ── email_send ───────────────────────────────────


@pytest.mark.asyncio
class TestEmailSend:
    async def test_send_success(self):
        with patch("pincer.tools.builtin.email_tool.get_settings") as mock_settings, \
             patch("aiosmtplib.send", new_callable=AsyncMock) as mock_send:
            s = MagicMock()
            s.email_from = "test@example.com"
            s.email_username = "test@example.com"
            s.email_smtp_host = "smtp.example.com"
            s.email_smtp_port = 587
            s.email_password.get_secret_value.return_value = "pass"
            mock_settings.return_value = s

            from pincer.tools.builtin.email_tool import email_send
            result = await email_send("user@example.com", "Test", "Hello")
            assert "sent" in result.lower()
            mock_send.assert_called_once()

    async def test_send_error(self):
        with patch("pincer.tools.builtin.email_tool.get_settings") as mock_settings, \
             patch("aiosmtplib.send", side_effect=Exception("SMTP error")):
            s = MagicMock()
            s.email_from = "test@example.com"
            s.email_username = "test@example.com"
            s.email_smtp_host = "smtp.example.com"
            s.email_smtp_port = 587
            s.email_password.get_secret_value.return_value = "pass"
            mock_settings.return_value = s

            from pincer.tools.builtin.email_tool import email_send
            result = await email_send("user@example.com", "Test", "Hello")
            assert "Error" in result


# ── email_search ─────────────────────────────────


@pytest.mark.asyncio
class TestEmailSearch:
    async def test_no_results(self):
        mock_client = _make_imap_client()
        mock_client.uid_search = AsyncMock(return_value=("OK", [b""]))

        with patch("pincer.tools.builtin.email_tool._get_imap_client", return_value=mock_client):
            from pincer.tools.builtin.email_tool import email_search
            result = await email_search("test query")
            assert "No emails" in result

    async def test_search_returns_uids(self):
        mock_client = _make_imap_client()

        header_bytes = (
            b"From: bob@example.com\r\n"
            b"Subject: Report Q1\r\n"
            b"Date: Thu, 27 Feb 2026 10:00:00 +0000\r\n"
        )

        mock_client.uid_search = AsyncMock(return_value=("OK", [b"500"]))

        async def mock_uid(command, *args):
            if command == "fetch":
                return ("OK", [b"1 FETCH (BODY[HEADER] {0})", bytearray(header_bytes), b")"])
            return ("OK", [b""])

        mock_client.uid = mock_uid

        with patch("pincer.tools.builtin.email_tool._get_imap_client", return_value=mock_client):
            from pincer.tools.builtin.email_tool import email_search
            result = await email_search("Report")
            assert "UID: 500" in result
            assert "bob@example.com" in result

    async def test_search_custom_folder(self):
        mock_client = _make_imap_client()

        header_bytes = (
            b"From: spammer@example.com\r\n"
            b"Subject: Win a prize\r\n"
            b"Date: Thu, 27 Feb 2026 10:00:00 +0000\r\n"
        )

        mock_client.uid_search = AsyncMock(return_value=("OK", [b"777"]))

        async def mock_uid(command, *args):
            if command == "fetch":
                return ("OK", [b"1 FETCH (BODY[HEADER] {0})", bytearray(header_bytes), b")"])
            return ("OK", [b""])

        mock_client.uid = mock_uid

        with patch("pincer.tools.builtin.email_tool._get_imap_client", return_value=mock_client):
            from pincer.tools.builtin.email_tool import email_search
            result = await email_search("prize", folder="[Gmail]/Spam")
            mock_client.select.assert_called_once_with('"[Gmail]/Spam"')
            assert "UID: 777" in result
            assert "spammer@example.com" in result


# ── email_read ───────────────────────────────────


@pytest.mark.asyncio
class TestEmailRead:
    async def test_read_success(self):
        raw_email = (
            b"From: alice@example.com\r\n"
            b"To: me@example.com\r\n"
            b"Subject: Test Email\r\n"
            b"Date: Thu, 27 Feb 2026 10:00:00 +0000\r\n"
            b"Message-ID: <abc@example.com>\r\n"
            b"MIME-Version: 1.0\r\n"
            b"Content-Type: text/plain\r\n"
            b"\r\n"
            b"Hello, this is the email body."
        )

        mock_client = _make_imap_client()

        async def mock_uid(command, *args):
            if command == "fetch":
                return ("OK", [b"1 FETCH (BODY[] {0})", bytearray(raw_email), b")"])
            return ("OK", [b""])

        mock_client.uid = mock_uid

        with patch("pincer.tools.builtin.email_tool._get_imap_client", return_value=mock_client):
            from pincer.tools.builtin.email_tool import email_read
            result = await email_read("12345")
            assert "UID: 12345" in result
            assert "alice@example.com" in result
            assert "Test Email" in result
            assert "Hello, this is the email body." in result

    async def test_read_not_found(self):
        mock_client = _make_imap_client()

        async def mock_uid(command, *args):
            return ("OK", [])

        mock_client.uid = mock_uid

        with patch("pincer.tools.builtin.email_tool._get_imap_client", return_value=mock_client):
            from pincer.tools.builtin.email_tool import email_read
            result = await email_read("99999")
            assert "not found" in result.lower() or "empty" in result.lower()

    async def test_read_error(self):
        with patch(
            "pincer.tools.builtin.email_tool._get_imap_client",
            side_effect=Exception("IMAP error"),
        ):
            from pincer.tools.builtin.email_tool import email_read
            result = await email_read("12345")
            assert "Error" in result

    async def test_read_body_truncation(self):
        long_body = "A" * 5000
        raw_email = (
            b"From: alice@example.com\r\n"
            b"Subject: Long\r\n"
            b"MIME-Version: 1.0\r\n"
            b"Content-Type: text/plain\r\n"
            b"\r\n"
            + long_body.encode()
        )

        mock_client = _make_imap_client()

        async def mock_uid(command, *args):
            if command == "fetch":
                return ("OK", [b"1 FETCH (BODY[] {0})", bytearray(raw_email), b")"])
            return ("OK", [b""])

        mock_client.uid = mock_uid

        with patch("pincer.tools.builtin.email_tool._get_imap_client", return_value=mock_client):
            from pincer.tools.builtin.email_tool import email_read
            result = await email_read("1", max_chars=100)
            assert "..." in result
            assert len(result) < 5000


# ── email_list_folders ───────────────────────────


@pytest.mark.asyncio
class TestEmailListFolders:
    async def test_list_success(self):
        folder_data = [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"',
            b'(\\HasNoChildren \\Junk) "/" "[Gmail]/Spam"',
        ]
        mock_client = _make_imap_client()
        mock_client.list = AsyncMock(return_value=("OK", folder_data))

        with patch("pincer.tools.builtin.email_tool._get_imap_client", return_value=mock_client):
            from pincer.tools.builtin.email_tool import email_list_folders
            result = await email_list_folders()
            assert "INBOX" in result
            assert "[Gmail]/Trash" in result
            assert "[Gmail]/Spam" in result
            assert "3" in result  # count

    async def test_list_empty(self):
        mock_client = _make_imap_client()
        mock_client.list = AsyncMock(return_value=("OK", []))

        with patch("pincer.tools.builtin.email_tool._get_imap_client", return_value=mock_client):
            from pincer.tools.builtin.email_tool import email_list_folders
            result = await email_list_folders()
            assert "No folders" in result

    async def test_list_error(self):
        with patch(
            "pincer.tools.builtin.email_tool._get_imap_client",
            side_effect=Exception("Connection failed"),
        ):
            from pincer.tools.builtin.email_tool import email_list_folders
            result = await email_list_folders()
            assert "Error" in result


# ── email_mark ───────────────────────────────────


@pytest.mark.asyncio
class TestEmailMark:
    async def test_mark_read(self):
        mock_client = _make_imap_client()

        store_calls = []

        async def mock_uid(command, *args):
            if command == "store":
                store_calls.append(args)
                return ("OK", [b""])
            return ("OK", [b""])

        mock_client.uid = mock_uid

        with patch("pincer.tools.builtin.email_tool._get_imap_client", return_value=mock_client):
            from pincer.tools.builtin.email_tool import email_mark
            result = await email_mark("100,200", "read")
            assert "2/2" in result
            assert "read" in result
            assert len(store_calls) == 2
            assert store_calls[0][1] == "+FLAGS"
            assert "\\Seen" in store_calls[0][2]

    async def test_mark_unread(self):
        mock_client = _make_imap_client()

        store_calls = []

        async def mock_uid(command, *args):
            if command == "store":
                store_calls.append(args)
                return ("OK", [b""])
            return ("OK", [b""])

        mock_client.uid = mock_uid

        with patch("pincer.tools.builtin.email_tool._get_imap_client", return_value=mock_client):
            from pincer.tools.builtin.email_tool import email_mark
            result = await email_mark("100", "unread")
            assert "1/1" in result
            assert store_calls[0][1] == "-FLAGS"

    async def test_mark_flag(self):
        mock_client = _make_imap_client()

        store_calls = []

        async def mock_uid(command, *args):
            if command == "store":
                store_calls.append(args)
                return ("OK", [b""])
            return ("OK", [b""])

        mock_client.uid = mock_uid

        with patch("pincer.tools.builtin.email_tool._get_imap_client", return_value=mock_client):
            from pincer.tools.builtin.email_tool import email_mark
            result = await email_mark("300", "flag")
            assert "1/1" in result
            assert "\\Flagged" in store_calls[0][2]

    async def test_mark_invalid_action(self):
        from pincer.tools.builtin.email_tool import email_mark
        result = await email_mark("100", "delete")
        assert "Invalid action" in result

    async def test_mark_error(self):
        with patch(
            "pincer.tools.builtin.email_tool._get_imap_client",
            side_effect=Exception("IMAP error"),
        ):
            from pincer.tools.builtin.email_tool import email_mark
            result = await email_mark("100", "read")
            assert "Error" in result


# ── email_move ───────────────────────────────────


@pytest.mark.asyncio
class TestEmailMove:
    async def test_move_success(self):
        mock_client = _make_imap_client()

        operations = []

        async def mock_uid(command, *args):
            operations.append((command, args))
            return ("OK", [b""])

        mock_client.uid = mock_uid

        with patch("pincer.tools.builtin.email_tool._get_imap_client", return_value=mock_client):
            from pincer.tools.builtin.email_tool import email_move
            result = await email_move("100", "Archive")
            assert "1/1" in result
            assert "Archive" in result

            copy_ops = [op for op in operations if op[0] == "copy"]
            store_ops = [op for op in operations if op[0] == "store"]
            assert len(copy_ops) == 1
            assert len(store_ops) == 1
            assert "\\Deleted" in store_ops[0][1][2]

    async def test_move_multiple(self):
        mock_client = _make_imap_client()

        async def mock_uid(command, *args):
            return ("OK", [b""])

        mock_client.uid = mock_uid

        with patch("pincer.tools.builtin.email_tool._get_imap_client", return_value=mock_client):
            from pincer.tools.builtin.email_tool import email_move
            result = await email_move("100,200,300", "Archive")
            assert "3/3" in result

    async def test_move_error(self):
        with patch(
            "pincer.tools.builtin.email_tool._get_imap_client",
            side_effect=Exception("IMAP error"),
        ):
            from pincer.tools.builtin.email_tool import email_move
            result = await email_move("100", "Archive")
            assert "Error" in result


# ── email_trash ──────────────────────────────────


@pytest.mark.asyncio
class TestEmailTrash:
    async def test_trash_success(self):
        mock_client = _make_imap_client()

        folder_data = [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"',
        ]
        mock_client.list = AsyncMock(return_value=("OK", folder_data))

        async def mock_uid(command, *args):
            return ("OK", [b""])

        mock_client.uid = mock_uid

        with patch("pincer.tools.builtin.email_tool._get_imap_client", return_value=mock_client):
            from pincer.tools.builtin.email_tool import email_trash
            result = await email_trash("100")
            assert "1/1" in result
            assert "[Gmail]/Trash" in result

    async def test_trash_fallback_folder(self):
        mock_client = _make_imap_client()

        folder_data = [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren) "/" "Trash"',
        ]
        mock_client.list = AsyncMock(return_value=("OK", folder_data))

        async def mock_uid(command, *args):
            return ("OK", [b""])

        mock_client.uid = mock_uid

        with patch("pincer.tools.builtin.email_tool._get_imap_client", return_value=mock_client):
            from pincer.tools.builtin.email_tool import email_trash
            result = await email_trash("100")
            assert "Trash" in result

    async def test_trash_error(self):
        with patch(
            "pincer.tools.builtin.email_tool._get_imap_client",
            side_effect=Exception("IMAP error"),
        ):
            from pincer.tools.builtin.email_tool import email_trash
            result = await email_trash("100")
            assert "Error" in result


# ── email_empty_folder ───────────────────────────


@pytest.mark.asyncio
class TestEmailEmptyFolder:
    async def test_empty_spam(self):
        mock_client = _make_imap_client()
        mock_client.uid_search = AsyncMock(return_value=("OK", [b"1 2 3"]))

        operations = []

        async def mock_uid(command, *args):
            operations.append((command, args))
            return ("OK", [b""])

        mock_client.uid = mock_uid

        with patch("pincer.tools.builtin.email_tool._get_imap_client", return_value=mock_client):
            from pincer.tools.builtin.email_tool import email_empty_folder
            result = await email_empty_folder("[Gmail]/Spam")
            assert "3/3 message(s)" in result
            assert "deleted" in result

            store_ops = [op for op in operations if op[0] == "store"]
            assert len(store_ops) == 3
            for op in store_ops:
                assert "\\Deleted" in op[1][2]

    async def test_empty_already_empty(self):
        mock_client = _make_imap_client()
        mock_client.uid_search = AsyncMock(return_value=("OK", [b""]))

        with patch("pincer.tools.builtin.email_tool._get_imap_client", return_value=mock_client):
            from pincer.tools.builtin.email_tool import email_empty_folder
            result = await email_empty_folder("[Gmail]/Trash")
            assert "already empty" in result

    async def test_empty_inbox_refused(self):
        from pincer.tools.builtin.email_tool import email_empty_folder
        result = await email_empty_folder("INBOX")
        assert "Refusing" in result

    async def test_empty_inbox_case_insensitive(self):
        from pincer.tools.builtin.email_tool import email_empty_folder
        result = await email_empty_folder("inbox")
        assert "Refusing" in result

    async def test_empty_bad_folder(self):
        mock_client = _make_imap_client()
        mock_client.select = AsyncMock(return_value=("NO", [b"folder not found"]))

        with patch("pincer.tools.builtin.email_tool._get_imap_client", return_value=mock_client):
            from pincer.tools.builtin.email_tool import email_empty_folder
            result = await email_empty_folder("NonExistent")
            assert "Could not select" in result

    async def test_empty_error(self):
        with patch(
            "pincer.tools.builtin.email_tool._get_imap_client",
            side_effect=Exception("IMAP error"),
        ):
            from pincer.tools.builtin.email_tool import email_empty_folder
            result = await email_empty_folder("[Gmail]/Spam")
            assert "Error" in result


# ── _parse_list_response ─────────────────────────


class TestParseListResponse:
    def test_parse_gmail_folders(self):
        from pincer.tools.builtin.email_tool import _parse_list_response
        data = [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"',
            b'(\\HasNoChildren \\Junk) "/" "[Gmail]/Spam"',
        ]
        parsed = _parse_list_response(data)
        assert len(parsed) == 3
        assert parsed[0] == (["\\HasNoChildren"], "INBOX")
        assert parsed[1][1] == "[Gmail]/Trash"
        assert "\\Trash" in parsed[1][0]

    def test_parse_empty_data(self):
        from pincer.tools.builtin.email_tool import _parse_list_response
        assert _parse_list_response([]) == []
        assert _parse_list_response([b""]) == []

    def test_parse_non_bytes_skipped(self):
        from pincer.tools.builtin.email_tool import _parse_list_response
        result = _parse_list_response(["not bytes", 42, None])  # type: ignore[list-item]
        assert result == []

    def test_parse_list_literal_folder_name(self):
        """LIST response with folder name as LITERAL+ (line ending {n}, next element bytearray)."""
        from pincer.tools.builtin.email_tool import _parse_list_response
        data = [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren \\Junk) "/" {12}',
            bytearray(b'[Gmail]/Spam'),
        ]
        parsed = _parse_list_response(data)
        assert len(parsed) == 2
        assert parsed[0][1] == "INBOX"
        assert parsed[1][1] == "[Gmail]/Spam"
        assert "\\Junk" in parsed[1][0]

    def test_parse_list_standalone_bytearray(self):
        """Standalone bytearray (no LIST line) treated as folder name with empty attrs."""
        from pincer.tools.builtin.email_tool import _parse_list_response
        data = [bytearray(b"Spam")]
        parsed = _parse_list_response(data)
        assert len(parsed) == 1
        assert parsed[0] == ([], "Spam")


# ── _parse_search_uids ──────────────────────────


class TestParseSearchUids:
    def test_numeric_tokens_only(self):
        from pincer.tools.builtin.email_tool import _parse_search_uids
        # Full * SEARCH line (robust parsing)
        result = _parse_search_uids([b"* SEARCH 1 2 3 4 5"])
        assert result == ["1", "2", "3", "4", "5"]

    def test_uid_only_line(self):
        from pincer.tools.builtin.email_tool import _parse_search_uids
        result = _parse_search_uids([b"100 200 300"])
        assert result == ["100", "200", "300"]

    def test_empty_data(self):
        from pincer.tools.builtin.email_tool import _parse_search_uids
        assert _parse_search_uids([]) == []

    def test_bytearray_first_line(self):
        from pincer.tools.builtin.email_tool import _parse_search_uids
        result = _parse_search_uids([bytearray(b"7 8 9")])
        assert result == ["7", "8", "9"]


# ── _quote_mailbox ────────────────────────────────


class TestQuoteMailbox:
    def test_simple_folder_unquoted(self):
        from pincer.tools.builtin.email_tool import _quote_mailbox
        assert _quote_mailbox("INBOX") == "INBOX"

    def test_folder_with_special_chars_quoted(self):
        from pincer.tools.builtin.email_tool import _quote_mailbox
        assert _quote_mailbox("[Gmail]/Spam") == '"[Gmail]/Spam"'


# ── _find_folder_by_attr ─────────────────────────


@pytest.mark.asyncio
class TestFindFolderByAttr:
    async def test_find_by_attribute(self):
        from pincer.tools.builtin.email_tool import _find_folder_by_attr, _TRASH_ATTRS, _TRASH_FALLBACKS
        mock_client = AsyncMock()
        mock_client.list = AsyncMock(return_value=("OK", [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"',
        ]))
        result = await _find_folder_by_attr(mock_client, _TRASH_ATTRS, _TRASH_FALLBACKS)
        assert result == "[Gmail]/Trash"

    async def test_fallback_by_name(self):
        from pincer.tools.builtin.email_tool import _find_folder_by_attr, _TRASH_ATTRS, _TRASH_FALLBACKS
        mock_client = AsyncMock()
        mock_client.list = AsyncMock(return_value=("OK", [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren) "/" "Trash"',
        ]))
        result = await _find_folder_by_attr(mock_client, _TRASH_ATTRS, _TRASH_FALLBACKS)
        assert result == "Trash"

    async def test_no_match_returns_first_fallback(self):
        from pincer.tools.builtin.email_tool import _find_folder_by_attr, _TRASH_ATTRS, _TRASH_FALLBACKS
        mock_client = AsyncMock()
        mock_client.list = AsyncMock(return_value=("OK", [
            b'(\\HasNoChildren) "/" "INBOX"',
        ]))
        result = await _find_folder_by_attr(mock_client, _TRASH_ATTRS, _TRASH_FALLBACKS)
        assert result == _TRASH_FALLBACKS[0]
