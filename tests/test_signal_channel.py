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
    s.signal_allowlist = ""
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


# ── allowlist filtering ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_allowlist_blocks_unknown_dm() -> None:
    from pincer.channels.signal_client import SignalMessage

    settings = _make_settings(signal_allowlist="+491111111111")
    ch = SignalChannel(settings)
    handler = AsyncMock(return_value="reply")
    ch._handler = handler
    ch._client = AsyncMock()

    msg = SignalMessage(
        source="+499999999999", timestamp=1, text="hi", is_group=False
    )
    await ch._process_signal_message(msg)

    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_allowlist_allows_known_dm() -> None:
    from pincer.channels.signal_client import SignalMessage

    settings = _make_settings(signal_allowlist="+491111111111")
    ch = SignalChannel(settings)
    handler = AsyncMock(return_value="reply")
    ch._handler = handler
    mock_client = AsyncMock()
    ch._client = mock_client

    msg = SignalMessage(
        source="+491111111111", timestamp=2, text="hi", is_group=False
    )
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
