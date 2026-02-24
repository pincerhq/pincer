"""Tests for Discord channel utilities and DiscordChannel class."""

from unittest.mock import MagicMock

from pydantic import SecretStr

from pincer.channels.base import ChannelType
from pincer.channels.discord_channel import (
    DiscordChannel,
    ResponseStyle,
    detect_response_style,
    make_embed,
    split_message,
    text_to_embed,
)


def test_split_message_short() -> None:
    """Message under 2000 chars returns single chunk."""
    text = "Hello, world!"
    chunks = split_message(text)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_split_message_exact_limit() -> None:
    """Message exactly at limit returns single chunk."""
    text = "x" * 2000
    chunks = split_message(text)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_split_message_at_newline() -> None:
    """Long message splits at newline near limit."""
    part1 = "a" * 1990
    part2 = "b" * 100
    text = part1 + "\n" + part2
    chunks = split_message(text)
    assert len(chunks) == 2
    assert chunks[0] == part1
    assert chunks[1] == part2


def test_split_message_at_space() -> None:
    """Long message without newline splits at space."""
    part1 = "a" * 1990
    part2 = "b" * 100
    text = part1 + " " + part2
    chunks = split_message(text)
    assert len(chunks) == 2
    assert chunks[0] == part1
    assert chunks[1] == part2


def test_split_message_hard_cut() -> None:
    """Long message without delimiters does hard cut."""
    text = "x" * 5000
    chunks = split_message(text)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk) <= 2000
    assert "".join(chunks) == text


def test_split_message_empty() -> None:
    """Empty string returns [""]."""
    chunks = split_message("")
    assert chunks == [""]


def test_detect_response_style_code() -> None:
    """Text wrapped in ``` -> CODE."""
    text = "```\ndef foo():\n    pass\n```"
    assert detect_response_style(text) == ResponseStyle.CODE


def test_detect_response_style_embed() -> None:
    """Long text with ## headings -> EMBED."""
    text = "x" * 850 + "\n## Section 1\nContent here\n\n## Section 2\nMore content"
    assert len(text) > 800
    assert detect_response_style(text) == ResponseStyle.EMBED


def test_detect_response_style_plain() -> None:
    """Short text -> PLAIN."""
    text = "Hello, world!"
    assert detect_response_style(text) == ResponseStyle.PLAIN


def test_detect_response_style_short_structured() -> None:
    """Short text with ## -> PLAIN (not long enough)."""
    text = "## Heading\nShort content"
    assert len(text) <= 800
    assert detect_response_style(text) == ResponseStyle.PLAIN


def test_make_embed_basic() -> None:
    """make_embed with title, description, color returns correct dict."""
    embed = make_embed(title="Test", description="Desc", color=0x123456)
    assert embed["title"] == "Test"
    assert embed["description"] == "Desc"
    assert embed["color"] == 0x123456


def test_make_embed_fields() -> None:
    """Fields capped at 25, values truncated to 1024."""
    fields = [{"name": f"f{i}", "value": "v" * 1500, "inline": False} for i in range(30)]
    embed = make_embed(fields=fields)
    assert len(embed["fields"]) == 25
    for f in embed["fields"]:
        assert len(f["value"]) <= 1024


def test_text_to_embed_sections() -> None:
    """Text with ## headings parsed into embed fields."""
    # First ## becomes title; subsequent ## become fields
    text = "Overview\n\n## Section A\nContent A\n\n## Section B\nContent B"
    embed = text_to_embed(text)
    assert embed["fields"] is not None
    assert len(embed["fields"]) == 2
    assert embed["fields"][0]["name"] == "Section A"
    assert "Content A" in embed["fields"][0]["value"]
    assert embed["fields"][1]["name"] == "Section B"
    assert "Content B" in embed["fields"][1]["value"]


def test_channel_type_is_discord() -> None:
    """DiscordChannel.channel_type == DISCORD."""
    assert DiscordChannel.channel_type == ChannelType.DISCORD


async def test_start_without_token_no_error() -> None:
    """DiscordChannel.start() with no token configured doesn't raise."""
    settings = MagicMock()
    settings.discord_bot_token = SecretStr("")

    channel = DiscordChannel(settings)
    await channel.start(MagicMock())
    # No exception raised
