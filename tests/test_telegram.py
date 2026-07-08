"""Tests for Telegram channel utilities."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from pincer.channels.telegram import TelegramChannel, split_message


def test_split_short_message() -> None:
    text = "Hello, world!"
    chunks = split_message(text)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_split_long_message() -> None:
    # Create a message longer than 4096 chars
    paragraph = "This is a test paragraph. " * 50  # ~1300 chars
    text = "\n\n".join([paragraph] * 5)  # ~6500 chars
    chunks = split_message(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 4096


def test_split_preserves_content() -> None:
    text = "Part 1\n\nPart 2\n\nPart 3"
    chunks = split_message(text)
    assert len(chunks) == 1
    assert "Part 1" in chunks[0]
    assert "Part 2" in chunks[0]
    assert "Part 3" in chunks[0]


def _make_channel() -> TelegramChannel:
    settings = MagicMock()
    settings.telegram_allowed_users = []
    channel = TelegramChannel(settings)
    channel._bot = AsyncMock()
    return channel


async def test_send_with_non_numeric_user_id_raises_once() -> None:
    """A canonical id (not a real Telegram chat id) must fail once, clearly — not twice (issue #162)."""
    channel = _make_channel()

    with pytest.raises(ValueError, match="invalid chat_id 'r_lutsiv'"):
        await channel.send("r_lutsiv", "hello")

    # The retry-on-failure path (send_message called a second time with
    # parse_mode=None) must never be reached — chat_id is invalid, not the markdown.
    channel._bot.send_message.assert_not_called()


async def test_send_file_with_non_numeric_user_id_raises_once(tmp_path) -> None:
    channel = _make_channel()
    file_path = tmp_path / "report.csv"
    file_path.write_text("a,b,c\n")

    with pytest.raises(ValueError, match="invalid chat_id 'r_lutsiv'"):
        await channel.send_file("r_lutsiv", str(file_path))

    channel._bot.send_document.assert_not_called()
    # send_file's except-fallback (self.send(...)) must not even be attempted —
    # it would fail on the exact same invalid id, doubling the exception.
    channel._bot.send_message.assert_not_called()


async def test_send_with_numeric_user_id_still_works() -> None:
    channel = _make_channel()

    await channel.send("123456789", "hello")

    channel._bot.send_message.assert_called_once_with(chat_id=123456789, text="hello")
