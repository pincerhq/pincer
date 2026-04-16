"""
Tests for the Slack channel implementation.

All tests mock slack-bolt / slack-sdk so no real Slack connection is made.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pincer.channels.base import ChannelType, IncomingMessage
from pincer.channels.slack import SlackChannel, split_message


# ── Helpers / fixtures ────────────────────────────────────────────────────────


def make_settings(
    bot_token: str = "xoxb-test",
    app_token: str = "xapp-test",
    allowlist: list[str] | None = None,
) -> Any:
    s = MagicMock()
    s.slack_bot_token.get_secret_value.return_value = bot_token
    s.slack_app_token.get_secret_value.return_value = app_token
    s.slack_user_allowlist = allowlist or []
    return s


def make_identity(pincer_id: str = "usr_abc123") -> Any:
    identity = AsyncMock()
    identity.resolve.return_value = pincer_id
    return identity


async def _noop_handler(msg: IncomingMessage) -> str:
    return "pong"


# ── Unit tests: channel properties ────────────────────────────────────────────


def test_channel_name() -> None:
    ch = SlackChannel(make_settings())
    assert ch.name == "slack"


def test_channel_type() -> None:
    ch = SlackChannel(make_settings())
    assert ch.channel_type == ChannelType.SLACK


# ── split_message ─────────────────────────────────────────────────────────────


def test_split_message_short() -> None:
    chunks = split_message("Hello world", max_len=100)
    assert chunks == ["Hello world"]


def test_split_message_long_splits_at_paragraphs() -> None:
    text = ("A" * 50 + "\n\n") * 100  # 5200 chars total
    chunks = split_message(text, max_len=200)
    assert all(len(c) <= 200 for c in chunks)
    assert len(chunks) > 1


def test_split_message_exact_boundary() -> None:
    text = "a" * 3900
    chunks = split_message(text, max_len=3900)
    assert chunks == [text]


def test_split_message_over_limit_hard_cuts() -> None:
    text = "x" * 5000
    chunks = split_message(text, max_len=3900)
    assert all(len(c) <= 3900 for c in chunks)
    assert "".join(chunks) == text


# ── start() with missing tokens ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_no_tokens_logs_warning() -> None:
    ch = SlackChannel(make_settings(bot_token="", app_token=""))
    with patch("pincer.channels.slack.logger") as mock_log:
        await ch.start(_noop_handler)
        mock_log.warning.assert_called_once()
    assert ch._app is None


@pytest.mark.asyncio
async def test_start_no_bot_token_skips() -> None:
    ch = SlackChannel(make_settings(bot_token="", app_token="xapp-x"))
    await ch.start(_noop_handler)
    assert ch._app is None


# ── start() with valid tokens ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_connects_socket_mode() -> None:
    """start() should create AsyncApp and call connect_async."""
    import sys

    # Build minimal mock modules for slack_bolt / slack_sdk so start() can import them
    mock_app_instance = MagicMock()
    mock_app_instance.event = MagicMock(return_value=lambda f: f)
    mock_app_instance.action = MagicMock(return_value=lambda f: f)
    mock_app_instance.client = AsyncMock()

    mock_web_client = AsyncMock()
    mock_web_client.auth_test = AsyncMock(return_value={"user_id": "U123", "user": "pincer"})

    mock_handler_instance = AsyncMock()
    mock_handler_instance.connect_async = AsyncMock()

    mock_async_app_cls = MagicMock(return_value=mock_app_instance)
    mock_socket_cls = MagicMock(return_value=mock_handler_instance)
    mock_web_client_cls = MagicMock(return_value=mock_web_client)

    # Fake the optional slack_bolt and slack_sdk modules
    fake_async_app_mod = MagicMock()
    fake_async_app_mod.AsyncApp = mock_async_app_cls
    fake_socket_mod = MagicMock()
    fake_socket_mod.AsyncSocketModeHandler = mock_socket_cls
    fake_web_client_mod = MagicMock()
    fake_web_client_mod.AsyncWebClient = mock_web_client_cls

    modules_to_fake = {
        "slack_bolt": MagicMock(),
        "slack_bolt.app": MagicMock(),
        "slack_bolt.app.async_app": fake_async_app_mod,
        "slack_bolt.adapter": MagicMock(),
        "slack_bolt.adapter.socket_mode": MagicMock(),
        "slack_bolt.adapter.socket_mode.async_handler": fake_socket_mod,
        "slack_sdk": MagicMock(),
        "slack_sdk.web": MagicMock(),
        "slack_sdk.web.async_client": fake_web_client_mod,
    }

    original: dict[str, Any] = {}
    for mod_name in modules_to_fake:
        original[mod_name] = sys.modules.get(mod_name)
        sys.modules[mod_name] = modules_to_fake[mod_name]  # type: ignore[assignment]

    try:
        ch = SlackChannel(make_settings())
        await ch.start(_noop_handler)
    finally:
        for mod_name, orig in original.items():
            if orig is None:
                sys.modules.pop(mod_name, None)
            else:
                sys.modules[mod_name] = orig

    assert ch._bot_user_id == "U123"
    mock_handler_instance.connect_async.assert_awaited_once()


# ── stop() ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_calls_close() -> None:
    ch = SlackChannel(make_settings())
    mock_socket_handler = AsyncMock()
    ch._socket_handler = mock_socket_handler
    await ch.stop()
    mock_socket_handler.close_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_no_handler_is_safe() -> None:
    ch = SlackChannel(make_settings())
    ch._socket_handler = None
    await ch.stop()  # Must not raise


# ── send() ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_short_message_single_call() -> None:
    mock_app = MagicMock()
    mock_app.client = AsyncMock()
    ch = SlackChannel(make_settings())
    ch._app = mock_app

    await ch.send("C1234567890", "Hello Slack!", channel_id="C1234567890")

    mock_app.client.chat_postMessage.assert_awaited_once()
    call_kwargs = mock_app.client.chat_postMessage.call_args.kwargs
    assert call_kwargs["channel"] == "C1234567890"
    assert "Hello Slack!" in call_kwargs["text"]


@pytest.mark.asyncio
async def test_send_long_message_multiple_calls() -> None:
    mock_app = MagicMock()
    mock_app.client = AsyncMock()
    ch = SlackChannel(make_settings())
    ch._app = mock_app

    long_text = ("paragraph content\n\n") * 300  # >3900 chars
    await ch.send("C1234", long_text, channel_id="C1234")

    assert mock_app.client.chat_postMessage.await_count > 1


@pytest.mark.asyncio
async def test_send_with_thread_ts() -> None:
    mock_app = MagicMock()
    mock_app.client = AsyncMock()
    ch = SlackChannel(make_settings())
    ch._app = mock_app

    await ch.send("C123", "threaded reply", channel_id="C123", thread_ts="1234567890.123")

    call_kwargs = mock_app.client.chat_postMessage.call_args.kwargs
    assert call_kwargs["thread_ts"] == "1234567890.123"


@pytest.mark.asyncio
async def test_send_user_id_opens_dm() -> None:
    """When given U... user ID, send() should call conversations_open."""
    mock_app = MagicMock()
    mock_app.client = AsyncMock()
    mock_app.client.conversations_open = AsyncMock(return_value={"channel": {"id": "D9999"}})
    ch = SlackChannel(make_settings())
    ch._app = mock_app

    await ch.send("U5678", "proactive message")

    mock_app.client.conversations_open.assert_awaited_once_with(users=["U5678"])
    call_kwargs = mock_app.client.chat_postMessage.call_args.kwargs
    assert call_kwargs["channel"] == "D9999"


@pytest.mark.asyncio
async def test_send_no_app_is_safe() -> None:
    ch = SlackChannel(make_settings())
    ch._app = None
    await ch.send("C123", "ignored")  # Must not raise


# ── _process_message ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dm_dispatches_to_handler() -> None:
    received: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> str:
        received.append(msg)
        return "response"

    mock_app = MagicMock()
    mock_app.client = AsyncMock()

    ch = SlackChannel(make_settings())
    ch._app = mock_app
    ch._bot_user_id = "U_BOT"
    ch._handler = handler
    ch._identity = make_identity("usr_test")

    mock_client = AsyncMock()
    mock_client.users_info = AsyncMock(
        return_value={"user": {"profile": {"display_name": "Alice", "real_name": "Alice"}}}
    )

    event = {
        "user": "U_ALICE",
        "channel": "D_DIRECT",
        "channel_type": "im",
        "text": "Hello Pincer",
        "ts": "1000.0",
    }

    await ch._process_message(event, mock_client)

    assert len(received) == 1
    assert received[0].text == "Hello Pincer"
    assert received[0].channel_type == ChannelType.SLACK
    assert "dm" in received[0].channel


@pytest.mark.asyncio
async def test_mention_strips_bot_id_from_text() -> None:
    received: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> str:
        received.append(msg)
        return "ok"

    ch = SlackChannel(make_settings())
    ch._app = MagicMock()
    ch._app.client = AsyncMock()
    ch._bot_user_id = "U_BOT"
    ch._handler = handler
    ch._identity = make_identity()

    mock_client = AsyncMock()
    mock_client.users_info = AsyncMock(
        return_value={"user": {"profile": {"display_name": "Bob", "real_name": "Bob"}}}
    )

    event = {
        "user": "U_BOB",
        "channel": "C_GENERAL",
        "channel_type": "channel",
        "text": "<@U_BOT> what is 2+2?",
        "ts": "2000.0",
    }

    await ch._process_message(event, mock_client)

    assert received[0].text == "what is 2+2?"


@pytest.mark.asyncio
async def test_channel_message_replied_in_thread() -> None:
    """Channel (non-DM) messages should get thread_ts set when calling send()."""
    mock_app = MagicMock()
    mock_app.client = AsyncMock()

    async def handler(msg: IncomingMessage) -> str:
        return "threaded"

    ch = SlackChannel(make_settings())
    ch._app = mock_app
    ch._bot_user_id = "U_BOT"
    ch._handler = handler
    ch._identity = make_identity()

    mock_client = AsyncMock()
    mock_client.users_info = AsyncMock(
        return_value={"user": {"profile": {"display_name": "Eve", "real_name": "Eve"}}}
    )

    event = {
        "user": "U_EVE",
        "channel": "C_ENG",
        "channel_type": "channel",
        "text": "<@U_BOT> summarize",
        "ts": "3000.0",
    }

    await ch._process_message(event, mock_client)

    call_kwargs = mock_app.client.chat_postMessage.call_args.kwargs
    assert call_kwargs["thread_ts"] == "3000.0"


@pytest.mark.asyncio
async def test_skip_own_messages() -> None:
    received: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> str:
        received.append(msg)
        return "should not happen"

    ch = SlackChannel(make_settings())
    ch._bot_user_id = "U_BOT"
    ch._handler = handler

    # Build a mock app with registered listeners
    mock_app = MagicMock()
    registered: dict[str, Any] = {}

    def event_decorator(event_type: str):
        def decorator(fn):
            registered[event_type] = fn
            return fn
        return decorator

    mock_app.event = event_decorator
    mock_app.action = MagicMock(return_value=lambda f: f)
    ch._app = mock_app
    ch._register_listeners()

    # Fire a "message" event from the bot itself
    event = {
        "user": "U_BOT",  # own message
        "channel": "D_DM",
        "channel_type": "im",
        "text": "I said this",
        "ts": "1.0",
    }
    await registered["message"](event=event, client=AsyncMock())
    assert received == []


@pytest.mark.asyncio
async def test_skip_bot_messages() -> None:
    received: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> str:
        received.append(msg)
        return ""

    ch = SlackChannel(make_settings())
    ch._bot_user_id = "U_BOT"
    ch._handler = handler

    mock_app = MagicMock()
    registered: dict[str, Any] = {}

    def event_decorator(event_type: str):
        def decorator(fn):
            registered[event_type] = fn
            return fn
        return decorator

    mock_app.event = event_decorator
    mock_app.action = MagicMock(return_value=lambda f: f)
    ch._app = mock_app
    ch._register_listeners()

    event = {
        "bot_id": "B_OTHER_BOT",
        "channel": "C_GENERAL",
        "text": "automated message",
        "ts": "1.0",
    }
    await registered["message"](event=event, client=AsyncMock())
    assert received == []


@pytest.mark.asyncio
async def test_skip_message_subtypes() -> None:
    received: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> str:
        received.append(msg)
        return ""

    ch = SlackChannel(make_settings())
    ch._bot_user_id = "U_BOT"
    ch._handler = handler

    mock_app = MagicMock()
    registered: dict[str, Any] = {}

    def event_decorator(event_type: str):
        def decorator(fn):
            registered[event_type] = fn
            return fn
        return decorator

    mock_app.event = event_decorator
    mock_app.action = MagicMock(return_value=lambda f: f)
    ch._app = mock_app
    ch._register_listeners()

    for subtype in ("message_changed", "message_deleted", "channel_join"):
        event = {
            "user": "U_HUMAN",
            "channel": "C_CHAN",
            "channel_type": "channel",
            "text": "edit",
            "subtype": subtype,
            "ts": "1.0",
        }
        await registered["message"](event=event, client=AsyncMock())

    assert received == []


# ── Allowlist ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_allowlist_blocks_unknown_user() -> None:
    received: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> str:
        received.append(msg)
        return ""

    ch = SlackChannel(make_settings(allowlist=["U_ALLOWED"]))
    ch._app = MagicMock()
    ch._app.client = AsyncMock()
    ch._bot_user_id = "U_BOT"
    ch._handler = handler

    mock_client = AsyncMock()
    event = {
        "user": "U_STRANGER",
        "channel": "D_DM",
        "channel_type": "im",
        "text": "hello",
        "ts": "1.0",
    }
    await ch._process_message(event, mock_client)
    assert received == []


@pytest.mark.asyncio
async def test_allowlist_permits_known_user() -> None:
    received: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> str:
        received.append(msg)
        return "hi"

    ch = SlackChannel(make_settings(allowlist=["U_ALLOWED"]))
    ch._app = MagicMock()
    ch._app.client = AsyncMock()
    ch._bot_user_id = "U_BOT"
    ch._handler = handler
    ch._identity = make_identity()

    mock_client = AsyncMock()
    mock_client.users_info = AsyncMock(
        return_value={"user": {"profile": {"display_name": "Alice", "real_name": "Alice"}}}
    )
    event = {
        "user": "U_ALLOWED",
        "channel": "D_DM",
        "channel_type": "im",
        "text": "hello",
        "ts": "1.0",
    }
    await ch._process_message(event, mock_client)
    assert len(received) == 1


# ── Identity resolution ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_identity_resolution_called() -> None:
    """_process_message resolves Slack user → pincer_user_id via identity resolver."""
    mock_identity = make_identity("usr_pincer_123")
    received: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> str:
        received.append(msg)
        return "ok"

    ch = SlackChannel(make_settings())
    ch._app = MagicMock()
    ch._app.client = AsyncMock()
    ch._bot_user_id = "U_BOT"
    ch._handler = handler
    ch._identity = mock_identity

    mock_client = AsyncMock()
    mock_client.users_info = AsyncMock(
        return_value={"user": {"profile": {"display_name": "Carol", "real_name": "Carol"}}}
    )
    event = {
        "user": "U_CAROL",
        "channel": "D_DM",
        "channel_type": "im",
        "text": "test identity",
        "ts": "5.0",
    }
    await ch._process_message(event, mock_client)

    mock_identity.resolve.assert_awaited_once_with(
        channel=ChannelType.SLACK,
        channel_user_id="U_CAROL",
        display_name="Carol",
    )
    assert received[0].pincer_user_id == "usr_pincer_123"


# ── Thread session keys ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dm_session_key_format() -> None:
    received: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> str:
        received.append(msg)
        return ""

    ch = SlackChannel(make_settings())
    ch._app = MagicMock()
    ch._app.client = AsyncMock()
    ch._bot_user_id = "U_BOT"
    ch._handler = handler
    ch._identity = make_identity()

    mock_client = AsyncMock()
    mock_client.users_info = AsyncMock(
        return_value={"user": {"profile": {"display_name": "Dave", "real_name": "Dave"}}}
    )
    event = {
        "user": "U_DAVE",
        "channel": "D_DM",
        "channel_type": "im",
        "text": "hello",
        "ts": "1.0",
    }
    await ch._process_message(event, mock_client)
    assert received[0].channel.startswith("slack-dm-")


@pytest.mark.asyncio
async def test_channel_message_session_key_format() -> None:
    received: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> str:
        received.append(msg)
        return ""

    ch = SlackChannel(make_settings())
    ch._app = MagicMock()
    ch._app.client = AsyncMock()
    ch._bot_user_id = "U_BOT"
    ch._handler = handler
    ch._identity = make_identity()

    mock_client = AsyncMock()
    mock_client.users_info = AsyncMock(
        return_value={"user": {"profile": {"display_name": "Eve", "real_name": "Eve"}}}
    )
    event = {
        "user": "U_EVE",
        "channel": "C_ENG",
        "channel_type": "channel",
        "text": "<@U_BOT> help",
        "ts": "9999.0",
    }
    await ch._process_message(event, mock_client)
    assert received[0].channel == "slack-thread-9999.0"


# ── Approval flow ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_approval_approve_flow() -> None:
    """Clicking ✅ resolves the pending future with True."""
    ch = SlackChannel(make_settings())
    mock_app = MagicMock()
    mock_app.client = AsyncMock()
    ch._app = mock_app

    approval_id = "test-approval-approve"

    async def send_and_wait() -> bool:
        return await ch.request_approval(
            channel_id="C_TEST",
            tool_name="shell_exec",
            tool_args={"command": "ls"},
            approval_id=approval_id,
            timeout=5.0,
        )

    # Simulate button click after a tiny delay
    async def click_approve() -> None:
        await asyncio.sleep(0.05)
        body = {
            "actions": [{"value": approval_id}],
            "channel": {"id": "C_TEST"},
            "message": {"ts": "999.0"},
            "user": {"name": "alice"},
        }
        await ch._resolve_approval(approval_id, approved=True, body=body, client=mock_app.client)

    result, _ = await asyncio.gather(send_and_wait(), click_approve())
    assert result is True


@pytest.mark.asyncio
async def test_approval_deny_flow() -> None:
    """Clicking ❌ resolves the pending future with False."""
    ch = SlackChannel(make_settings())
    mock_app = MagicMock()
    mock_app.client = AsyncMock()
    ch._app = mock_app

    approval_id = "test-approval-deny"

    async def send_and_wait() -> bool:
        return await ch.request_approval(
            channel_id="C_TEST",
            tool_name="email_send",
            tool_args={"to": "alice@test.com"},
            approval_id=approval_id,
            timeout=5.0,
        )

    async def click_deny() -> None:
        await asyncio.sleep(0.05)
        body = {
            "actions": [{"value": approval_id}],
            "channel": {"id": "C_TEST"},
            "message": {"ts": "888.0"},
            "user": {"name": "bob"},
        }
        await ch._resolve_approval(approval_id, approved=False, body=body, client=mock_app.client)

    result, _ = await asyncio.gather(send_and_wait(), click_deny())
    assert result is False


@pytest.mark.asyncio
async def test_approval_timeout_returns_false() -> None:
    ch = SlackChannel(make_settings())
    ch._app = MagicMock()
    ch._app.client = AsyncMock()

    result = await ch.request_approval(
        channel_id="C_NONE",
        tool_name="dangerous_tool",
        tool_args={},
        approval_id="timeout-id",
        timeout=0.1,
    )
    assert result is False
