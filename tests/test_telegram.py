"""Tests for Telegram channel utilities."""

from pincer.channels.telegram import split_message


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
