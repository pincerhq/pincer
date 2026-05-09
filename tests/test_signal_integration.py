"""Integration tests for Signal channel receive → handler → reply flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pincer.channels.signal import SignalChannel
from pincer.channels.signal_client import SignalMessage


def _make_settings(**kwargs) -> MagicMock:
    s = MagicMock()
    s.signal_api_url = "http://localhost:8080"
    s.signal_phone_number = "+491234567890"
    s.signal_allowlist = ""
    s.signal_group_reply = "all"
    s.signal_receive_mode = "poll"
    s.signal_poll_interval = 2
    s.agent_name = "Pincer"
    s.openai_api_key = MagicMock()
    s.openai_api_key.get_secret_value.return_value = ""
    for k, v in kwargs.items():
        setattr(s, k, v)
    return s


@pytest.mark.asyncio
async def test_receive_to_reply_flow() -> None:
    """Full receive → handler → reply flow."""
    settings = _make_settings()
    ch = SignalChannel(settings)

    replies: list[tuple[str, str]] = []

    async def mock_handler(incoming):
        return f"Echo: {incoming.text}"

    mock_client = AsyncMock()

    async def mock_send_message(recipient, text):
        replies.append((recipient, text))

    mock_client.send_message.side_effect = mock_send_message
    mock_client.send_typing_indicator = AsyncMock()

    ch._handler = mock_handler
    ch._client = mock_client

    msg = SignalMessage(
        source="+491111111111",
        timestamp=100,
        text="Hello Pincer",
        is_group=False,
    )
    await ch._process_signal_message(msg)

    assert len(replies) == 1
    assert replies[0][0] == "+491111111111"
    assert "Echo: Hello Pincer" in replies[0][1]


@pytest.mark.asyncio
async def test_allowlist_blocks_dm() -> None:
    settings = _make_settings(signal_allowlist="+491111111111")
    ch = SignalChannel(settings)
    handler = AsyncMock(return_value="reply")
    ch._handler = handler
    ch._client = AsyncMock()

    msg = SignalMessage(
        source="+499876543210",  # not in allowlist
        timestamp=200,
        text="intrusion attempt",
        is_group=False,
    )
    await ch._process_signal_message(msg)
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_all_mode_replies_without_mention() -> None:
    settings = _make_settings(signal_group_reply="all")
    ch = SignalChannel(settings)

    received: list[str] = []

    async def mock_handler(incoming):
        received.append(incoming.text)
        return "OK"

    mock_client = AsyncMock()
    ch._handler = mock_handler
    ch._client = mock_client

    msg = SignalMessage(
        source="+491111111111",
        timestamp=300,
        text="Random group chat message",
        is_group=True,
        group_id="grp-1",
    )
    await ch._process_signal_message(msg)
    assert len(received) == 1


@pytest.mark.asyncio
async def test_group_disabled_mode_ignores_all() -> None:
    settings = _make_settings(signal_group_reply="disabled")
    ch = SignalChannel(settings)
    handler = AsyncMock(return_value="reply")
    ch._handler = handler
    ch._client = AsyncMock()

    msg = SignalMessage(
        source="+491111111111",
        timestamp=400,
        text="Hey Pincer",
        is_group=True,
        group_id="grp-2",
    )
    await ch._process_signal_message(msg)
    handler.assert_not_awaited()
