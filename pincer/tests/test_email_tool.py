"""Tests for email tool."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
class TestEmailCheck:
    async def test_no_unread(self):
        mock_client = AsyncMock()
        mock_client.wait_hello_from_server = AsyncMock()
        mock_client.login = AsyncMock()
        mock_client.select = AsyncMock()
        mock_client.search = AsyncMock(return_value=("OK", [b""]))
        mock_client.logout = AsyncMock()

        with patch("pincer.tools.builtin.email_tool._get_imap_client", return_value=mock_client):
            from pincer.tools.builtin.email_tool import email_check
            result = await email_check()
            assert "No unread" in result

    async def test_error_handling(self):
        with patch(
            "pincer.tools.builtin.email_tool._get_imap_client",
            side_effect=Exception("Connection failed"),
        ):
            from pincer.tools.builtin.email_tool import email_check
            result = await email_check()
            assert "Error" in result


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


@pytest.mark.asyncio
class TestEmailSearch:
    async def test_no_results(self):
        mock_client = AsyncMock()
        mock_client.wait_hello_from_server = AsyncMock()
        mock_client.login = AsyncMock()
        mock_client.select = AsyncMock()
        mock_client.search = AsyncMock(return_value=("OK", [b""]))
        mock_client.logout = AsyncMock()

        with patch("pincer.tools.builtin.email_tool._get_imap_client", return_value=mock_client):
            from pincer.tools.builtin.email_tool import email_search
            result = await email_search("test query")
            assert "No emails" in result
