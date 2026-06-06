"""
Tests for the Microsoft Teams channel implementation.

All tests mock the microsoft-teams-apps SDK and uvicorn so no real server is started
and no real Teams connection is made.
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pincer.channels.base import ChannelType, IncomingMessage
from pincer.channels.microsoft_teams import (
    MAX_TEAMS_MESSAGE_LENGTH,
    MicrosoftTeamsChannel,
    split_message,
)

# ── Helpers / fixtures ────────────────────────────────────────────────────────


def make_settings(
    app_id: str = "app-123",
    app_password: str = "secret-xyz",
    port: int = 3978,
    allowlist: list[str] | None = None,
) -> Any:
    s = MagicMock()
    s.teams_app_id = app_id
    s.teams_app_password.get_secret_value.return_value = app_password
    s.teams_port = port
    s.teams_user_allowlist = allowlist or []
    return s


def make_activity(
    text: str = "hello",
    user_id: str = "user-id-1",
    aad_object_id: str | None = "aad-1",
    conversation_id: str = "conv-1",
    conversation_type: str = "personal",
    activity_id: str = "act-1",
) -> Any:
    activity = MagicMock()
    activity.text = text
    activity.id = activity_id
    activity.from_ = MagicMock()
    activity.from_.id = user_id
    activity.from_.aad_object_id = aad_object_id
    activity.conversation = MagicMock()
    activity.conversation.id = conversation_id
    activity.conversation.conversation_type = conversation_type
    return activity


def make_ctx(activity: Any) -> Any:
    ctx = MagicMock()
    ctx.activity = activity
    ctx.send = AsyncMock()
    return ctx


async def _echo_handler(msg: IncomingMessage) -> str:
    return f"echo: {msg.text}"


# ── Channel properties ────────────────────────────────────────────────────────


def test_channel_name() -> None:
    ch = MicrosoftTeamsChannel(make_settings())
    assert ch.name == "teams"


def test_channel_type() -> None:
    ch = MicrosoftTeamsChannel(make_settings())
    assert ch.channel_type == ChannelType.TEAMS


def test_teams_channel_type_registered() -> None:
    assert ChannelType.TEAMS == "teams"


# ── split_message ─────────────────────────────────────────────────────────────


def test_split_message_short() -> None:
    assert split_message("Hello world", max_len=100) == ["Hello world"]


def test_split_message_long_splits_at_paragraphs() -> None:
    text = ("A" * 50 + "\n\n") * 100
    chunks = split_message(text, max_len=200)
    assert all(len(c) <= 200 for c in chunks)
    assert len(chunks) > 1


def test_split_message_exact_boundary() -> None:
    text = "a" * MAX_TEAMS_MESSAGE_LENGTH
    assert split_message(text) == [text]


def test_split_message_over_limit_hard_cuts() -> None:
    text = "x" * 20000
    chunks = split_message(text, max_len=8000)
    assert all(len(c) <= 8000 for c in chunks)
    assert "".join(chunks) == text


# ── start() with missing credentials ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_no_credentials_logs_warning() -> None:
    ch = MicrosoftTeamsChannel(make_settings(app_id="", app_password=""))
    with patch("pincer.channels.microsoft_teams.logger") as mock_log:
        await ch.start(_echo_handler)
        mock_log.warning.assert_called_once()
    assert ch._app is None


@pytest.mark.asyncio
async def test_start_no_password_skips() -> None:
    ch = MicrosoftTeamsChannel(make_settings(app_password=""))
    await ch.start(_echo_handler)
    assert ch._app is None


# ── start() with valid credentials ────────────────────────────────────────────


def _fake_teams_modules() -> tuple[dict[str, Any], dict[str, Any]]:
    """Build fake microsoft_teams + fastapi + uvicorn modules for import in start()."""
    mock_app_instance = MagicMock()
    mock_app_instance.on_message = MagicMock(return_value=lambda f: f)
    mock_app_instance.initialize = AsyncMock()
    mock_app_instance.send = AsyncMock()

    mock_app_cls = MagicMock(return_value=mock_app_instance)
    mock_adapter_cls = MagicMock()

    fake_apps_mod = MagicMock()
    fake_apps_mod.App = mock_app_cls
    fake_apps_mod.FastAPIAdapter = mock_adapter_cls

    fake_api_mod = MagicMock()
    fake_api_mod.MessageActivityInput = MagicMock()

    mock_server_instance = MagicMock()
    mock_server_instance.serve = AsyncMock()
    fake_uvicorn_mod = MagicMock()
    fake_uvicorn_mod.Config = MagicMock()
    fake_uvicorn_mod.Server = MagicMock(return_value=mock_server_instance)

    fake_fastapi_mod = MagicMock()
    fake_fastapi_mod.FastAPI = MagicMock()

    modules = {
        "uvicorn": fake_uvicorn_mod,
        "fastapi": fake_fastapi_mod,
        "microsoft_teams": MagicMock(),
        "microsoft_teams.apps": fake_apps_mod,
        "microsoft_teams.api": fake_api_mod,
    }
    handles = {
        "app_cls": mock_app_cls,
        "app_instance": mock_app_instance,
        "server_instance": mock_server_instance,
    }
    return modules, handles


@pytest.mark.asyncio
async def test_start_builds_app_and_serves() -> None:
    modules, handles = _fake_teams_modules()
    original: dict[str, Any] = {}
    for name, mod in modules.items():
        original[name] = sys.modules.get(name)
        sys.modules[name] = mod
    try:
        ch = MicrosoftTeamsChannel(make_settings())
        await ch.start(_echo_handler)
        # Let the background serve task get scheduled
        assert ch._app is not None
        handles["app_instance"].initialize.assert_awaited_once()
        # App constructed with the right credentials
        _, kwargs = handles["app_cls"].call_args
        assert kwargs["client_id"] == "app-123"
        assert kwargs["client_secret"] == "secret-xyz"
        assert ch._server_task is not None
    finally:
        if ch._server_task is not None:
            ch._server_task.cancel()
        for name, orig in original.items():
            if orig is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = orig


@pytest.mark.asyncio
async def test_start_import_error_is_handled() -> None:
    ch = MicrosoftTeamsChannel(make_settings())
    # Force the SDK import to fail
    with (
        patch.dict(sys.modules, {"microsoft_teams.apps": None}),
        patch("pincer.channels.microsoft_teams.logger") as mock_log,
    ):
        await ch.start(_echo_handler)
        mock_log.error.assert_called_once()
    assert ch._app is None


# ── stop() ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_signals_server_exit() -> None:
    ch = MicrosoftTeamsChannel(make_settings())
    mock_server = MagicMock()

    async def _serve() -> None:
        return None

    import asyncio

    ch._server = mock_server
    ch._server_task = asyncio.create_task(_serve())
    await ch.stop()
    assert mock_server.should_exit is True
    assert ch._server_task is None


@pytest.mark.asyncio
async def test_stop_no_server_is_safe() -> None:
    ch = MicrosoftTeamsChannel(make_settings())
    await ch.stop()  # Must not raise


# ── Session key design ────────────────────────────────────────────────────────


def test_session_key_personal_dm() -> None:
    ch = MicrosoftTeamsChannel(make_settings())
    activity = make_activity(conversation_type="personal", aad_object_id="aad-99")
    assert ch._make_session_key(activity) == "teams-dm-aad-99"


def test_session_key_group_chat() -> None:
    ch = MicrosoftTeamsChannel(make_settings())
    activity = make_activity(conversation_type="groupChat", conversation_id="chat-7")
    assert ch._make_session_key(activity) == "teams-chat-chat-7"


def test_session_key_channel_new_message() -> None:
    ch = MicrosoftTeamsChannel(make_settings())
    activity = make_activity(
        conversation_type="channel", conversation_id="19:abc@thread.tacv2", activity_id="act-42"
    )
    assert ch._make_session_key(activity) == "teams-thread-act-42"


def test_session_key_channel_existing_thread() -> None:
    ch = MicrosoftTeamsChannel(make_settings())
    activity = make_activity(
        conversation_type="channel",
        conversation_id="19:abc@thread.tacv2;messageid=1700000000000",
    )
    assert ch._make_session_key(activity) == "teams-thread-1700000000000"


def test_session_key_falls_back_to_id_when_no_aad() -> None:
    ch = MicrosoftTeamsChannel(make_settings())
    activity = make_activity(conversation_type="personal", aad_object_id=None, user_id="raw-id")
    assert ch._make_session_key(activity) == "teams-dm-raw-id"


# ── _handle_activity ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_activity_builds_incoming_message() -> None:
    ch = MicrosoftTeamsChannel(make_settings())
    captured: dict[str, Any] = {}

    async def handler(msg: IncomingMessage) -> str:
        captured["msg"] = msg
        return "reply text"

    ch._handler = handler
    activity = make_activity(text="do a thing", conversation_type="personal", aad_object_id="aad-1")
    ctx = make_ctx(activity)

    await ch._handle_activity(ctx)

    msg = captured["msg"]
    assert msg.user_id == "aad-1"
    assert msg.text == "do a thing"
    assert msg.channel == "teams-dm-aad-1"
    assert msg.channel_type == ChannelType.TEAMS
    assert msg.raw is activity
    ctx.send.assert_awaited_once_with("reply text")


@pytest.mark.asyncio
async def test_handle_activity_strips_mention() -> None:
    ch = MicrosoftTeamsChannel(make_settings())
    captured: dict[str, Any] = {}

    async def handler(msg: IncomingMessage) -> str:
        captured["text"] = msg.text
        return ""

    ch._handler = handler
    activity = make_activity(text="<at>Pincer Bot</at> what is the weather")
    await ch._handle_activity(make_ctx(activity))
    assert captured["text"] == "what is the weather"


@pytest.mark.asyncio
async def test_handle_activity_empty_text_after_strip_is_ignored() -> None:
    ch = MicrosoftTeamsChannel(make_settings())
    handler = AsyncMock()
    ch._handler = handler
    activity = make_activity(text="<at>Pincer Bot</at>   ")
    await ch._handle_activity(make_ctx(activity))
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_activity_allowlist_blocks() -> None:
    ch = MicrosoftTeamsChannel(make_settings(allowlist=["allowed-aad"]))
    handler = AsyncMock(return_value="x")
    ch._handler = handler
    activity = make_activity(aad_object_id="blocked-aad")
    ctx = make_ctx(activity)
    await ch._handle_activity(ctx)
    handler.assert_not_awaited()
    ctx.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_activity_allowlist_allows() -> None:
    ch = MicrosoftTeamsChannel(make_settings(allowlist=["allowed-aad"]))
    handler = AsyncMock(return_value="ok")
    ch._handler = handler
    activity = make_activity(aad_object_id="allowed-aad")
    ctx = make_ctx(activity)
    await ch._handle_activity(ctx)
    handler.assert_awaited_once()
    ctx.send.assert_awaited_once_with("ok")


@pytest.mark.asyncio
async def test_handle_activity_stores_conversation_ref() -> None:
    ch = MicrosoftTeamsChannel(make_settings())
    ch._handler = AsyncMock(return_value="")
    activity = make_activity(aad_object_id="aad-1", conversation_id="conv-xyz")
    await ch._handle_activity(make_ctx(activity))
    assert ch._conversation_refs["aad-1"] == "conv-xyz"


@pytest.mark.asyncio
async def test_handle_activity_long_response_chunks() -> None:
    ch = MicrosoftTeamsChannel(make_settings())
    long = "p" * (MAX_TEAMS_MESSAGE_LENGTH + 100)
    ch._handler = AsyncMock(return_value=long)
    ctx = make_ctx(make_activity())
    await ch._handle_activity(ctx)
    assert ctx.send.await_count >= 2


@pytest.mark.asyncio
async def test_handle_activity_handler_exception_sends_error() -> None:
    ch = MicrosoftTeamsChannel(make_settings())

    async def boom(_msg: IncomingMessage) -> str:
        raise RuntimeError("kaboom")

    ch._handler = boom
    ctx = make_ctx(make_activity())
    await ch._handle_activity(ctx)
    ctx.send.assert_awaited_once()
    assert "went wrong" in ctx.send.await_args.args[0]


# ── send() (proactive) ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_no_app_is_safe() -> None:
    ch = MicrosoftTeamsChannel(make_settings())
    await ch.send("aad-1", "hi")  # Must not raise (no app)


@pytest.mark.asyncio
async def test_send_uses_stored_conversation_ref() -> None:
    ch = MicrosoftTeamsChannel(make_settings())
    ch._app = MagicMock()
    ch._app.send = AsyncMock()
    ch._conversation_refs["aad-1"] = "conv-9"

    fake_api = MagicMock()
    fake_api.MessageActivityInput = MagicMock(side_effect=lambda text: {"text": text})
    with patch.dict(sys.modules, {"microsoft_teams.api": fake_api}):
        await ch.send("aad-1", "hello there")

    ch._app.send.assert_awaited_once()
    args = ch._app.send.await_args.args
    assert args[0] == "conv-9"


@pytest.mark.asyncio
async def test_send_no_conversation_ref_warns() -> None:
    ch = MicrosoftTeamsChannel(make_settings())
    ch._app = MagicMock()
    ch._app.send = AsyncMock()
    with patch("pincer.channels.microsoft_teams.logger") as mock_log:
        await ch.send("unknown-user", "hi")
        mock_log.warning.assert_called_once()
    ch._app.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_explicit_conversation_id_override() -> None:
    ch = MicrosoftTeamsChannel(make_settings())
    ch._app = MagicMock()
    ch._app.send = AsyncMock()
    fake_api = MagicMock()
    fake_api.MessageActivityInput = MagicMock(side_effect=lambda text: {"text": text})
    with patch.dict(sys.modules, {"microsoft_teams.api": fake_api}):
        await ch.send("ignored", "hi", conversation_id="explicit-conv")
    assert ch._app.send.await_args.args[0] == "explicit-conv"
