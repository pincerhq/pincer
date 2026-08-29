"""Unit tests for SignalChannel."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pincer.channels.signal import SignalChannel, _split_message

# ── _split_message ────────────────────────────────────────────────────────────


def test_split_message_short() -> None:
    assert _split_message("hello") == ["hello"]


def test_split_message_exact_limit() -> None:
    text = "a" * 6000
    chunks = _split_message(text, max_len=6000)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_split_message_long() -> None:
    text = "x" * 12001
    chunks = _split_message(text, max_len=6000)
    assert len(chunks) == 3
    assert all(len(c) <= 6000 for c in chunks)
    assert "".join(chunks) == text


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_settings(**kwargs) -> MagicMock:
    s = MagicMock()
    s.signal_api_url = "http://localhost:8080"
    s.signal_phone_number = "+491234567890"
    s.signal_group_reply = "mention_only"
    s.signal_receive_mode = "poll"
    s.signal_poll_interval = 2
    s.agent_name = "Pincer"
    s.openai_api_key = MagicMock()
    s.openai_api_key.get_secret_value.return_value = ""
    for k, v in kwargs.items():
        setattr(s, k, v)
    return s


# ── SignalChannel.send ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_dm() -> None:
    settings = _make_settings()
    ch = SignalChannel(settings)
    mock_client = AsyncMock()
    ch._client = mock_client

    await ch.send("+491111111111", "Hello!", recipient="+491111111111")

    mock_client.send_typing_indicator.assert_awaited_once_with("+491111111111")
    mock_client.send_message.assert_awaited_once_with("+491111111111", "Hello!")


@pytest.mark.asyncio
async def test_send_group() -> None:
    settings = _make_settings()
    ch = SignalChannel(settings)
    mock_client = AsyncMock()
    ch._client = mock_client

    await ch.send("src", "Group reply", is_group=True, group_id="grp-abc")

    mock_client.send_group_message.assert_awaited_once_with("grp-abc", "Group reply")
    mock_client.send_typing_indicator.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_splits_long_message() -> None:
    settings = _make_settings()
    ch = SignalChannel(settings)
    mock_client = AsyncMock()
    ch._client = mock_client

    long_text = "z" * 13000
    await ch.send("+491111111111", long_text, recipient="+491111111111")

    assert mock_client.send_message.await_count == 3


@pytest.mark.asyncio
async def test_send_before_start_raises() -> None:
    """Delivery failure must be visible to the caller, not silently swallowed (issue #162)."""
    ch = SignalChannel(_make_settings())

    with pytest.raises(RuntimeError, match="called before start"):
        await ch.send("+491111111111", "Hello!")


@pytest.mark.asyncio
async def test_send_message_failure_propagates() -> None:
    settings = _make_settings()
    ch = SignalChannel(settings)
    mock_client = AsyncMock()
    mock_client.send_message.side_effect = RuntimeError("signal-cli unreachable")
    ch._client = mock_client

    with pytest.raises(RuntimeError, match="signal-cli unreachable"):
        await ch.send("+491111111111", "Hello!", recipient="+491111111111")


# ── guest check (identity map) ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_guest_blocked_when_identity_configured() -> None:
    from pincer.channels.signal_client import SignalMessage

    settings = _make_settings(signal_guests_allowed=False)
    identity = AsyncMock()
    identity.is_guest = AsyncMock(return_value=True)
    ch = SignalChannel(settings, identity=identity)
    handler = AsyncMock(return_value="reply")
    ch._handler = handler
    ch._client = AsyncMock()

    msg = SignalMessage(source="+491111111111", timestamp=3, text="hi", is_group=False)
    await ch._process_signal_message(msg)

    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_guest_allowed_when_flag_true() -> None:
    from pincer.channels.signal_client import SignalMessage

    settings = _make_settings(signal_guests_allowed=True)
    identity = AsyncMock()
    identity.is_guest = AsyncMock(return_value=True)
    ch = SignalChannel(settings, identity=identity)
    handler = AsyncMock(return_value="reply")
    ch._handler = handler
    ch._client = AsyncMock()

    msg = SignalMessage(source="+491111111111", timestamp=4, text="hi", is_group=False)
    await ch._process_signal_message(msg)

    handler.assert_awaited_once()
    identity.is_guest.assert_not_called()


@pytest.mark.asyncio
async def test_no_guest_check_when_no_identity_configured() -> None:
    from pincer.channels.signal_client import SignalMessage

    settings = _make_settings(signal_guests_allowed=False)
    ch = SignalChannel(settings)
    assert ch._identity is None
    handler = AsyncMock(return_value="reply")
    ch._handler = handler
    ch._client = AsyncMock()

    msg = SignalMessage(source="+491111111111", timestamp=5, text="hi", is_group=False)
    await ch._process_signal_message(msg)

    handler.assert_awaited_once()


# ── group mention filter ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_group_mention_only_ignores_unreferenced() -> None:
    from pincer.channels.signal_client import SignalMessage

    settings = _make_settings(signal_group_reply="mention_only")
    ch = SignalChannel(settings)
    handler = AsyncMock(return_value="reply")
    ch._handler = handler
    ch._client = AsyncMock()

    msg = SignalMessage(
        source="+491111111111",
        timestamp=3,
        text="Hello everyone",
        is_group=True,
        group_id="grp-1",
    )
    await ch._process_signal_message(msg)

    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_mention_only_processes_mention() -> None:
    from pincer.channels.signal_client import SignalMessage

    settings = _make_settings(signal_group_reply="mention_only")
    ch = SignalChannel(settings)
    handler = AsyncMock(return_value="reply")
    ch._handler = handler
    mock_client = AsyncMock()
    ch._client = mock_client

    msg = SignalMessage(
        source="+491111111111",
        timestamp=4,
        text="Hey Pincer, what's the weather?",
        is_group=True,
        group_id="grp-1",
    )
    await ch._process_signal_message(msg)

    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_group_mention_guest_rejected_when_identity_configured() -> None:
    """Group mention from a sender not in the identity map, guests not allowed → rejected."""
    from pincer.channels.base import ChannelType
    from pincer.channels.signal_client import SignalMessage

    settings = _make_settings(signal_guests_allowed=False, signal_group_reply="mention_only")
    identity = AsyncMock()
    identity.is_guest = AsyncMock(return_value=True)
    ch = SignalChannel(settings, identity=identity)
    handler = AsyncMock(return_value="reply")
    ch._handler = handler
    ch._client = AsyncMock()

    msg = SignalMessage(
        source="+491111111111",
        timestamp=6,
        text="Hey Pincer, what's up?",
        is_group=True,
        group_id="grp-1",
    )
    await ch._process_signal_message(msg)

    handler.assert_not_awaited()
    identity.is_guest.assert_awaited_once_with(ChannelType.SIGNAL, "+491111111111")


@pytest.mark.asyncio
async def test_group_mention_guest_allowed_when_flag_true() -> None:
    """Group mention from a sender not in the identity map, guests allowed → responds."""
    from pincer.channels.signal_client import SignalMessage

    settings = _make_settings(signal_guests_allowed=True, signal_group_reply="mention_only")
    identity = AsyncMock()
    identity.is_guest = AsyncMock(return_value=True)
    ch = SignalChannel(settings, identity=identity)
    handler = AsyncMock(return_value="reply")
    ch._handler = handler
    ch._client = AsyncMock()

    msg = SignalMessage(
        source="+491111111111",
        timestamp=7,
        text="Hey Pincer, what's up?",
        is_group=True,
        group_id="grp-1",
    )
    await ch._process_signal_message(msg)

    handler.assert_awaited_once()
    identity.is_guest.assert_not_called()


@pytest.mark.asyncio
async def test_group_mention_only_ignores_unreferenced_even_with_identity_configured() -> None:
    """Unmentioned group chatter is dropped by the mention filter before any identity lookup."""
    from pincer.channels.signal_client import SignalMessage

    settings = _make_settings(signal_guests_allowed=False, signal_group_reply="mention_only")
    identity = AsyncMock()
    ch = SignalChannel(settings, identity=identity)
    handler = AsyncMock(return_value="reply")
    ch._handler = handler
    ch._client = AsyncMock()

    msg = SignalMessage(
        source="+491111111111",
        timestamp=8,
        text="Hello everyone",
        is_group=True,
        group_id="grp-1",
    )
    await ch._process_signal_message(msg)

    handler.assert_not_awaited()
    identity.is_guest.assert_not_called()


# ── deduplication ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dedup_by_timestamp() -> None:
    from pincer.channels.signal_client import SignalMessage

    settings = _make_settings()
    ch = SignalChannel(settings)
    handler = AsyncMock(return_value="reply")
    ch._handler = handler
    mock_client = AsyncMock()
    ch._client = mock_client

    msg = SignalMessage(source="+491111111111", timestamp=999, text="dupe", is_group=False)
    await ch._process_signal_message(msg)
    await ch._process_signal_message(msg)  # second time — should be deduplicated

    assert handler.await_count == 1


@pytest.mark.asyncio
async def test_process_message_reply_send_failure_does_not_propagate() -> None:
    """A failed reply send must not blow up the receive loop for the rest of a batch."""
    from pincer.channels.signal_client import SignalMessage

    settings = _make_settings()
    ch = SignalChannel(settings)
    ch._handler = AsyncMock(return_value="reply")
    mock_client = AsyncMock()
    mock_client.send_message.side_effect = RuntimeError("signal-cli unreachable")
    ch._client = mock_client

    msg = SignalMessage(source="+491111111111", timestamp=42, text="hi", is_group=False)
    await ch._process_signal_message(msg)  # must not raise


# ── stop cancels tasks ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_cancels_tasks() -> None:
    import asyncio

    settings = _make_settings()
    ch = SignalChannel(settings)

    async def _never_stop() -> None:
        await asyncio.sleep(9999)

    task = asyncio.create_task(_never_stop())
    ch._tasks = [task]
    ch._client = AsyncMock()

    await ch.stop()

    assert task.cancelled()
    assert ch._tasks == []
