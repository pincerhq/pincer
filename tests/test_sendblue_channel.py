"""Sendblue's public HTTP boundary, real agent loop, and provider acceptance contract."""

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import SecretStr

from pincer.channels.base import ChannelType
from pincer.channels.middleware import IdentityMiddleware
from pincer.channels.router import ChannelRouter
from pincer.channels.sendblue import SendblueChannel
from pincer.core.agent import Agent

LINE = "+15555550100"
SENDER = "+15555550101"
SECRET = "test-webhook-secret"


def event(**changes):
    return {
        "message_handle": "fixture-inbound-1",
        "is_outbound": False,
        "status": "RECEIVED",
        "from_number": SENDER,
        "to_number": LINE,
        "content": "Hello!",
        "group_id": "",
        **changes,
    }


@pytest.fixture
def sendblue_settings(settings):
    settings.sendblue_api_key = SecretStr("test-key")
    settings.sendblue_api_secret = SecretStr("test-secret")
    settings.sendblue_signing_secret = SecretStr(SECRET)
    settings.sendblue_from_number = LINE
    settings.sendblue_allow_from = [SENDER]
    return settings


@pytest.fixture
async def channel(sendblue_settings):
    instance = SendblueChannel(sendblue_settings)
    await instance.start(AsyncMock(return_value=""))
    yield instance
    await instance.stop()
    from pincer.db.engine import dispose_engines

    await dispose_engines()


@asynccontextmanager
async def api_client(channel):
    from unittest.mock import patch

    from pincer.api.server import create_app

    with patch("pincer.api.server.get_settings_relaxed", return_value=channel._settings):
        app = create_app()
        app.state.sendblue_channel = channel
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            yield client


@pytest.fixture
async def client(channel):
    async with api_client(channel) as client:
        yield client


async def post(client, payload, secret=SECRET):
    return await client.post("/api/apps/sendblue/webhook", json=payload, headers={"sb-signing-secret": secret})


async def test_http_agent_reply_persisted_and_proactive_send(
    channel, client, settings, mock_llm, session_manager, cost_tracker, tool_registry, identity_resolver
):
    """Only the LLM and remote provider are fixtures; channel, agent, identity and SQLite are real."""
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    identity = IdentityMiddleware(identity_resolver)
    canonical_ids = []

    async def handle(message):
        message = await identity(message)
        canonical_ids.append(message.pincer_user_id)
        response = await agent.handle_message(message.pincer_user_id, message.channel, message.text)
        return response.text

    received = []

    async def provider(request):
        assert request.headers["sb-api-key-id"] == "test-key"
        assert request.headers["sb-api-secret-key"] == "test-secret"
        received.append(await request.json())
        return web.json_response({"message_handle": "fixture-outbound-1", "status": "QUEUED", "error_code": 0})

    provider_app = web.Application()
    provider_app.router.add_post("/api/send-message", provider)
    async with TestServer(provider_app) as server:
        await channel._client.aclose()
        channel._client = httpx.AsyncClient(
            base_url=str(server.make_url("/")),
            headers={"sb-api-key-id": "test-key", "sb-api-secret-key": "test-secret"},
        )
        channel._handler = handle
        assert (await post(client, event())).status_code == 200
        await asyncio.wait_for(channel._queue.join(), 10)
        assert received == [
            {
                "number": SENDER,
                "from_number": LINE,
                "content": (
                    "Hello! I'm Pincer.\n\nBy the way — what's your name, "
                    "and what will you mainly use me for? Feel free to also mention your preferred language."
                ),
            }
        ]
        session = await session_manager.get_or_create(canonical_ids[0], "sendblue")
        assert any(m.content == "Hello!" for m in session.messages)
        assert any(m.content == received[0]["content"] for m in session.messages)
        assert (await post(client, event())).status_code == 200
        await channel._queue.join()
        assert len(received) == 1
        router = ChannelRouter(identity_resolver)
        router.register(ChannelType.SENDBLUE, channel)
        assert await router.send(ChannelType.SENDBLUE, SENDER, "Reminder")
        assert received[-1]["content"] == "Reminder"


@pytest.mark.parametrize(
    "change,status",
    [
        ({"is_outbound": True}, 200),
        ({"is_outbound": "false"}, 200),
        ({"status": "DELIVERED"}, 200),
        ({"group_id": "group-1"}, 200),
        ({"to_number": "+15555550999"}, 200),
        ({"from_number": "+15555550999"}, 200),
        ({"message_handle": ""}, 400),
        ({"content": {"bad": True}}, 400),
        ({"media_url": []}, 400),
    ],
)
async def test_untrusted_and_unsupported_events_never_reach_agent(channel, client, change, status):
    assert (await post(client, event(**change))).status_code == status
    await channel._queue.join()
    channel._handler.assert_not_called()


async def test_authentication_and_bounded_payload(channel, client):
    assert (await post(client, event(), secret="wrong")).status_code == 401
    assert (await post(client, [])).status_code == 400
    response = await client.post("/api/apps/sendblue/webhook", content="{", headers={"sb-signing-secret": SECRET})
    assert response.status_code == 400
    assert (await post(client, event(content="x" * 70000))).status_code == 413
    channel._handler.assert_not_called()


async def test_queue_pressure_does_not_poison_retry(channel, client):
    channel._worker.cancel()
    await asyncio.gather(channel._worker, return_exceptions=True)
    channel._worker = None
    for n in range(128):
        assert (await post(client, event(message_handle=f"m-{n}"))).status_code == 200
    assert (await post(client, event(message_handle="retry"))).status_code == 503
    channel._queue.get_nowait()
    channel._queue.task_done()
    assert (await post(client, event(message_handle="retry"))).status_code == 200


async def test_media_is_not_downloaded(channel, client):
    assert (await post(client, event(content=None, media_url="http://169.254.169.254/secret"))).status_code == 200
    await channel._queue.join()
    text = channel._handler.call_args.args[0].text
    assert "supports text only" in text
    assert "169.254" not in text


@pytest.mark.parametrize(
    "response",
    [
        {"status": "ERROR", "message_handle": "m"},
        {"status": "QUEUED", "message_handle": "m", "error_code": 400},
        {"status": "QUEUED"},
        [],
    ],
)
async def test_provider_rejection_is_not_success(channel, response):
    calls = []

    def provider(request):
        calls.append(request)
        return httpx.Response(200, json=response)

    await channel._client.aclose()
    channel._client = httpx.AsyncClient(transport=httpx.MockTransport(provider), base_url="https://api.sendblue.com")
    with pytest.raises(RuntimeError, match="did not confirm"):
        await channel.send(SENDER, "hello")
    assert len(calls) == 1


async def test_timeout_no_retry_and_chunking(channel):
    calls = []

    def provider(request):
        calls.append(json.loads(request.content))
        if len(calls) == 2:
            raise httpx.ReadTimeout("ambiguous acceptance")
        return httpx.Response(200, json={"status": "QUEUED", "message_handle": "m"})

    await channel._client.aclose()
    channel._client = httpx.AsyncClient(transport=httpx.MockTransport(provider), base_url="https://api.sendblue.com")
    with pytest.raises(RuntimeError, match="unconfirmed"):
        await channel.send(SENDER, "x" * 4500)
    assert len(calls) == 2
    assert len(calls[0]["content"]) == 2000
    with pytest.raises(ValueError, match="allowlist"):
        await channel.send("+15555550999", "blocked")
    assert len(calls) == 2


async def test_missing_config_fails_closed(settings):
    with pytest.raises(ValueError, match="requires"):
        await SendblueChannel(settings).start(AsyncMock())


async def test_failed_handler_is_not_replayed(channel, client):
    channel._handler = AsyncMock(side_effect=RuntimeError("fixture failure"))
    channel.send = AsyncMock()
    assert (await post(client, event())).status_code == 200
    await channel._queue.join()
    assert (await post(client, event())).status_code == 200
    await channel._queue.join()
    channel._handler.assert_awaited_once()


async def test_invalid_allowlist_fails_before_opening_listener(sendblue_settings):
    sendblue_settings.sendblue_allow_from = ["not-a-phone"]
    channel = SendblueChannel(sendblue_settings)
    with pytest.raises(ValueError, match="allowlist entries"):
        await channel.start(AsyncMock())
    assert channel._client is None
    assert channel._worker is None


async def test_stop_cancels_active_and_queued_work_before_closing_client(channel, client):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(message):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            assert not channel._client.is_closed
            cancelled.set()

    channel._handler = handler
    await post(client, event())
    await asyncio.wait_for(entered.wait(), 2)
    await post(client, event(message_handle="queued"))
    await asyncio.wait_for(channel.stop(), 2)
    assert cancelled.is_set()
    assert not channel.running
    assert not channel._tasks
    await asyncio.wait_for(channel._queue.join(), 2)
    assert (await post(client, event(message_handle="after-stop"))).status_code == 503
    await channel.stop()


async def test_identity_roster_blocks_allowlisted_guest(channel, client, settings):
    from pincer.core.identity import IdentityResolver

    known = "+15555550102"
    identity = IdentityResolver(settings.db_path, f"owner@sendblue:{known}")
    await identity.ensure_table()
    router = ChannelRouter(identity)
    router.register(ChannelType.SENDBLUE, channel)
    await router.rebuild_identity_map()
    channel._identity = identity
    channel._settings.sendblue_allow_from = ["*"]
    assert (await post(client, event())).status_code == 200
    await channel._queue.join()
    channel._handler.assert_not_called()
    assert not channel._seen
    assert (await post(client, event(from_number=known))).status_code == 200
    await channel._queue.join()
    channel._handler.assert_awaited_once()
    assert channel._handler.call_args.args[0].user_id == known


@pytest.mark.parametrize("content", [None, "", " \n\t"])
async def test_empty_messages_are_not_queued_or_remembered(channel, client, content):
    assert (await post(client, event(content=content))).status_code == 200
    await channel._queue.join()
    channel._handler.assert_not_called()
    assert not channel._seen


async def test_duplicate_cache_evicts_oldest_and_retains_recent(channel, client):
    channel._seen.update((f"old-{n}", None) for n in range(4096))
    assert (await post(client, event())).status_code == 200
    await channel._queue.join()
    assert len(channel._seen) == 4096
    assert "old-0" not in channel._seen
    assert (await post(client, event(message_handle="old-4095"))).status_code == 200
    await channel._queue.join()
    channel._handler.assert_awaited_once()
    assert (await post(client, event(message_handle="old-0"))).status_code == 200
    await channel._queue.join()
    assert channel._handler.await_count == 2
    assert len(channel._seen) == 4096


async def test_worker_drains_uninitialized_handler_and_recovers(channel, client, caplog):
    channel._handler = None
    channel.send = AsyncMock()
    assert (await post(client, event())).status_code == 200
    await asyncio.wait_for(channel._queue.join(), 2)
    assert "RuntimeError" in caplog.text
    assert "fixture-inbound-1" in caplog.text
    assert not channel._worker.done()
    channel._handler = AsyncMock(return_value="")
    assert (await post(client, event(message_handle="next"))).status_code == 200
    await asyncio.wait_for(channel._queue.join(), 2)
    channel._handler.assert_awaited_once()


async def test_send_requires_started_channel(sendblue_settings):
    with pytest.raises(RuntimeError, match="not started"):
        await SendblueChannel(sendblue_settings).send(SENDER, "hello")


async def test_invalid_recipient_never_calls_provider(channel, monkeypatch):
    request = AsyncMock()
    monkeypatch.setattr(channel._client, "post", request)
    with pytest.raises(ValueError, match="E.164"):
        await channel.send("not-a-phone", "hello")
    request.assert_not_called()


async def test_unsupported_attachments_never_call_provider(channel, monkeypatch):
    request = AsyncMock()
    monkeypatch.setattr(channel._client, "post", request)
    with pytest.raises(NotImplementedError, match="text only"):
        await channel.send_file(SENDER, "/unused/file.pdf")
    with pytest.raises(NotImplementedError, match="text only"):
        await channel.send_photo_from_bytes(SENDER, b"image")
    request.assert_not_called()


@pytest.mark.parametrize(
    "enabled,start_error",
    [(True, None), (False, None), (True, ValueError)],
)
@pytest.mark.parametrize("prior_policy", [None, False, True])
async def test_cli_sendblue_startup_registers_live_reply_and_proactive_routes(
    sendblue_settings,
    mock_llm,
    session_manager,
    cost_tracker,
    tool_registry,
    monkeypatch,
    enabled,
    start_error,
    capsys,
    prior_policy,
):
    """Run real CLI channel wiring; stop before unrelated schedulers/signals start."""
    import importlib
    from types import SimpleNamespace

    from pincer.db.engine import dispose_engines

    cli = importlib.import_module("pincer.cli.run")
    settings = sendblue_settings
    settings.sendblue_enabled = enabled
    settings.telegram_bot_token = SecretStr("")
    settings.discord_bot_token = SecretStr("")
    settings.slack_bot_token = SecretStr("")
    settings.teams_app_id = ""
    settings.whatsapp_enabled = False
    settings.signal_enabled = False
    settings.voice_enabled = False
    settings.voice_outbound_enabled = False
    settings.identity_map = f"owner@sendblue:{SENDER}"
    settings.skills_index_watch_interval_seconds = 0
    telegram = None
    if start_error:
        # An earlier channel must survive a missing SDK, invalid config or busy port.
        settings.telegram_bot_token = SecretStr("fixture-token")
        telegram = MagicMock(name="telegram-channel")
        telegram.name = "telegram"
        telegram.start = AsyncMock()
        telegram.stop = AsyncMock()
        monkeypatch.setattr("pincer.channels.telegram.TelegramChannel", lambda *a, **kw: telegram)
        if start_error is ValueError:
            settings.sendblue_api_key = SecretStr("")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    original_callback = AsyncMock(return_value=prior_policy) if prior_policy is not None else None
    agent._approval_callback = original_callback
    rate_limiter = AsyncMock()
    core = SimpleNamespace(
        session_mgr=session_manager,
        cost_tracker=cost_tracker,
        audit_logger=None,
        rate_limiter=rate_limiter,
        llm=mock_llm,
        memory_store=None,
        tools=tool_registry,
        mcp_manager=None,
        agent=agent,
    )
    monkeypatch.setattr(cli, "_build_core", AsyncMock(return_value=core))
    monkeypatch.setattr(cli, "_port_in_use", lambda *_: False)
    maps = []
    register_tools = cli._register_channel_bound_tools

    def capture_channel_map(tools, channel_map):
        maps.append(channel_map)
        register_tools(tools, channel_map)

    monkeypatch.setattr(cli, "_register_channel_bound_tools", capture_channel_map)
    routers = []
    rebuild = ChannelRouter.rebuild_identity_map

    class ChannelsStarted(Exception):
        pass

    async def stop_before_scheduler(router):
        await rebuild(router)
        routers.append(router)
        raise ChannelsStarted

    monkeypatch.setattr(ChannelRouter, "rebuild_identity_map", stop_before_scheduler)
    try:
        if not enabled:
            await cli._start_agent(settings, None, cli._Shutdown())
            assert maps == [{}]
            assert not routers
            mock_llm.complete.assert_not_called()
            assert agent._approval_callback is original_callback
            return

        with pytest.raises(ChannelsStarted):
            await cli._start_agent(settings, None, cli._Shutdown())
        if start_error:
            assert maps == [{"telegram": telegram}]
            assert routers[0].channels == {ChannelType.TELEGRAM: telegram}
            telegram.start.assert_awaited_once()
            telegram.stop.assert_not_awaited()
            output = capsys.readouterr().out
            assert "Sendblue failed to start: Sendblue requires API key and API secret" in output
            assert "sensitive fixture detail" not in output
            return
        for other in ("discord", "signal", "slack", "web", ""):
            assert await agent._approval_callback("risky", {}, "fixture-user", other) is (
                True if prior_policy is None else prior_policy
            )
        if original_callback is not None:
            assert original_callback.await_count == 5
        assert await agent._approval_callback("risky", {}, "fixture-user", "sendblue") is False
        channel = maps[0]["sendblue"]
        router = routers[0]
        assert router.channels == {ChannelType.SENDBLUE: channel}
        assert channel._identity is agent.identity_resolver
        assert channel._worker is not None and not channel._worker.done()
        received = []

        async def provider(request):
            assert request.headers["sb-api-key-id"] == "test-key"
            assert request.headers["sb-api-secret-key"] == "test-secret"
            received.append(await request.json())
            return web.json_response({"status": "QUEUED", "message_handle": "cli-reply"})

        app = web.Application()
        app.router.add_post("/api/send-message", provider)
        async with TestServer(app) as server:
            await channel._client.aclose()
            channel._client = httpx.AsyncClient(
                base_url=str(server.make_url("/")),
                headers={"sb-api-key-id": "test-key", "sb-api-secret-key": "test-secret"},
            )
            async with api_client(channel) as client:
                assert (await post(client, event())).status_code == 200
                await asyncio.wait_for(channel._queue.join(), 10)
            assert len(received) == 1
            assert received[0]["number"] == SENDER
            assert received[0]["from_number"] == LINE
            assert "Hello! I'm Pincer." in received[0]["content"]
            canonical = await channel._identity.find(ChannelType.SENDBLUE, SENDER)
            assert canonical is not None
            rate_limiter.check_message.assert_awaited_once_with(canonical)
            session = await session_manager.get_or_create(canonical, "sendblue")
            assert any(m.content == "Hello!" for m in session.messages)
            assert any("Hello! I'm Pincer." in m.content for m in session.messages if m.content)
            assert await router.send(ChannelType.SENDBLUE, SENDER, "CLI reminder")
            assert received[-1] == {"number": SENDER, "from_number": LINE, "content": "CLI reminder"}

            # A Sendblue-only deployment must deny approval-required tools.
            from pincer.llm.base import LLMResponse, ToolCall

            executed = []

            async def risky() -> str:
                executed.append(True)
                return "unsafe side effect"

            tool_registry.register(name="risky", description="Requires approval", handler=risky, require_approval=True)
            mock_llm.complete.side_effect = [
                LLMResponse(
                    content="",
                    model="test",
                    input_tokens=1,
                    output_tokens=1,
                    stop_reason="tool_use",
                    tool_calls=[ToolCall(id="approval-check", name="risky", arguments={})],
                ),
                LLMResponse(
                    content="Action declined", model="test", input_tokens=1, output_tokens=1, stop_reason="end_turn"
                ),
            ]
            async with api_client(channel) as client:
                assert (await post(client, event(message_handle="approval", content="Run risky"))).status_code == 200
                await asyncio.wait_for(channel._queue.join(), 10)
            assert not executed
            session = await session_manager.get_or_create(canonical, "sendblue")
            assert any(
                m.tool_call_id == "approval-check" and "declined" in (m.content or "").lower() for m in session.messages
            )
            assert "Action declined" in received[-1]["content"]
    finally:
        for channel_map in maps:
            for channel in channel_map.values():
                await channel.stop()
        await dispose_engines()


async def test_invalid_header_bytes_fail_authentication(channel, client):
    response = await client.post(
        "/api/apps/sendblue/webhook", content=b"{}", headers=[(b"sb-signing-secret", b"\xff\xfe")]
    )
    assert response.status_code == 401
    channel._handler.assert_not_called()


async def test_unknown_charset_cannot_raise_server_error(channel, client):
    headers = {"sb-signing-secret": SECRET, "Content-Type": "application/json; charset=nope"}
    response = await client.post("/api/apps/sendblue/webhook", content=b"{", headers=headers)
    assert response.status_code == 400
    channel._handler.assert_not_called()
    response = await client.post("/api/apps/sendblue/webhook", content=json.dumps(event()).encode(), headers=headers)
    assert response.status_code == 200
    await channel._queue.join()
    channel._handler.assert_awaited_once()


async def test_empty_allowlist_starts_but_denies_all(sendblue_settings):
    sendblue_settings.sendblue_allow_from = []
    channel = SendblueChannel(sendblue_settings)
    handler = AsyncMock(return_value="")
    try:
        await channel.start(handler)
        async with api_client(channel) as client:
            assert (await post(client, event())).status_code == 200
        await channel._queue.join()
        handler.assert_not_called()
        assert not channel._seen
        with pytest.raises(ValueError, match="allowlist"):
            await channel.send(SENDER, "blocked")
    finally:
        await channel.stop()


@pytest.mark.parametrize(
    "value,expected",
    [
        (SENDER, [SENDER]),
        (f" {SENDER},+15555550102 ", [SENDER, "+15555550102"]),
        ("", []),
        ("  ", []),
        (json.dumps([SENDER]), [SENDER]),
    ],
)
def test_env_allowlist_accepts_csv_json_and_blank(monkeypatch, value, expected):
    from pincer.config import Settings

    monkeypatch.setenv("PINCER_SENDBLUE_ALLOW_FROM", value)
    cfg = Settings(_env_file=None, anthropic_api_key="fixture")
    assert cfg.sendblue_allow_from == expected


@pytest.mark.parametrize("status", ["REGISTERED", "PENDING", "ACCEPTED"])
async def test_accepted_status_does_not_truncate_reply(channel, status):
    calls = []

    def provider(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"status": status, "message_handle": "accepted"})

    await channel._client.aclose()
    channel._client = httpx.AsyncClient(transport=httpx.MockTransport(provider), base_url="https://api.sendblue.com")
    await channel.send(SENDER, "x" * 4500)
    assert [len(c["content"]) for c in calls] == [2000, 2000, 500]


async def test_identity_phone_normalization_and_proactive_wire_number(channel, client, settings):
    from pincer.core.identity import IdentityResolver

    identity = IdentityResolver(settings.db_path, f"owner@sendblue:{SENDER[1:]}")
    await identity.ensure_table()
    router = ChannelRouter(identity)
    router.register(ChannelType.SENDBLUE, channel)
    await router.rebuild_identity_map()
    channel._identity = identity
    assert not await identity.is_guest(ChannelType.SENDBLUE, SENDER)
    canonical = await identity.find(ChannelType.SENDBLUE, SENDER)
    targets = await identity.get_all_channels(canonical)
    assert targets[ChannelType.SENDBLUE] == SENDER[1:]
    calls = []

    def provider(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "QUEUED", "message_handle": "m"})

    await channel._client.aclose()
    channel._client = httpx.AsyncClient(transport=httpx.MockTransport(provider), base_url="https://api.sendblue.com")
    assert await router.send_to_user(canonical, "proactive", prefer=ChannelType.SENDBLUE)
    assert calls[0]["number"] == SENDER


@pytest.mark.parametrize("sender", ["person@example.com", "12345", "+15555550999"])
async def test_unsupported_sender_ignored_before_shape_validation(channel, client, sender):
    response = await post(client, event(from_number=sender, content={}, media_url=[]))
    assert response.status_code == 200
    assert not channel._seen
    channel._handler.assert_not_called()


async def test_other_sender_progresses_while_first_sender_waits(channel, client):
    channel._settings.sendblue_allow_from = ["*"]
    blocked = asyncio.Event()
    other = asyncio.Event()
    release = asyncio.Event()
    order = []

    async def handler(message):
        if message.user_id == SENDER and message.text == "first":
            blocked.set()
            await release.wait()
        order.append(message.text)
        if message.user_id != SENDER:
            other.set()
        return ""

    channel._handler = handler
    try:
        await post(client, event(content="first"))
        await asyncio.wait_for(blocked.wait(), 2)
        await post(client, event(message_handle="second", content="second"))
        await post(client, event(message_handle="other", from_number="+15555550102", content="other"))
        await asyncio.wait_for(other.wait(), 2)
        assert order == ["other"]
    finally:
        release.set()
        await asyncio.wait_for(channel._queue.join(), 3)
    assert order == ["other", "first", "second"]


async def test_handler_error_notice_and_logs_exclude_sensitive_exception(channel, client, caplog):
    channel._handler = AsyncMock(side_effect=RuntimeError("private token and message text"))
    sends = []

    def provider(request):
        sends.append(json.loads(request.content))
        return httpx.Response(200, json={"message_handle": "notice", "status": "ACCEPTED"})

    await channel._client.aclose()
    channel._client = httpx.AsyncClient(transport=httpx.MockTransport(provider), base_url="https://api.sendblue.com")
    await post(client, event())
    await channel._queue.join()
    await post(client, event())
    await channel._queue.join()
    assert len(sends) == 1
    assert sends[0]["number"] == SENDER
    assert "couldn't process" in sends[0]["content"]
    assert "handler failed (RuntimeError)" in caplog.text
    assert "private token" not in caplog.text


async def test_ambiguous_reply_logs_type_without_sending_another_notice(channel, client, caplog):
    channel._handler = AsyncMock(return_value="reply")
    calls = []

    def provider(request):
        calls.append(request)
        raise httpx.ReadTimeout("private token")

    await channel._client.aclose()
    channel._client = httpx.AsyncClient(transport=httpx.MockTransport(provider), base_url="https://api.sendblue.com")
    await post(client, event())
    await channel._queue.join()
    assert len(calls) == 1
    assert "reply failed (RuntimeError)" in caplog.text
    assert "private token" not in caplog.text


async def test_shared_api_auth_status_and_inactive_channel(channel):
    from unittest.mock import patch

    from pincer.api.server import create_app

    channel._settings.auth_disabled = False
    with patch("pincer.api.server.get_settings_relaxed", return_value=channel._settings):
        app = create_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            assert (await post(client, event())).status_code == 503
            app.state.sendblue_channel = channel
            assert (await post(client, event(), secret="wrong")).status_code == 401
            assert (await post(client, event())).status_code == 200
            assert (await client.get("/api/status")).status_code == 401
            channel._settings.auth_disabled = True
            assert (await client.get("/api/status")).json()["channels"]["sendblue"] is True
            await channel.stop()
            assert (await client.get("/api/status")).json()["channels"]["sendblue"] is False


async def test_live_shared_api_socket_authentication(channel):
    import socket
    from unittest.mock import patch

    import uvicorn

    from pincer.api.server import create_app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    with patch("pincer.api.server.get_settings_relaxed", return_value=channel._settings):
        app = create_app()
    app.state.sendblue_channel = channel
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(3):
            while not server.started:
                await asyncio.sleep(0.01)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"POST /api/apps/sendblue/webhook HTTP/1.1\r\nHost: localhost\r\n"
                b"sb-signing-secret: \xff\xfe\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            assert b" 401 " in await asyncio.wait_for(reader.readline(), 2)
        finally:
            writer.close()
            await writer.wait_closed()
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            assert (await post(client, event())).status_code == 200
        await channel._queue.join()
        channel._handler.assert_awaited_once()
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 3)
        sock.close()


def test_sendblue_doctor_settings(sendblue_settings):
    from pincer.security.doctor import CheckStatus, SecurityDoctor

    cfg = sendblue_settings
    doc = SecurityDoctor()
    assert doc._check_sendblue_credentials(cfg).status == CheckStatus.SKIPPED
    cfg.sendblue_enabled = True
    assert doc._check_sendblue_credentials(cfg).status == CheckStatus.PASS
    cfg.sendblue_signing_secret = SecretStr("")
    assert doc._check_sendblue_credentials(cfg).status == CheckStatus.CRITICAL
    cfg.sendblue_allow_from = ["*"]
    assert doc._check_sendblue_access(cfg).status == CheckStatus.WARNING
    cfg.sendblue_allow_from = []
    assert "denies all" in doc._check_sendblue_access(cfg).message
    cfg.sendblue_allow_from = ["invalid"]
    assert doc._check_sendblue_access(cfg).status == CheckStatus.CRITICAL
    cfg.sendblue_allow_from = [SENDER]
    assert doc._check_sendblue_access(cfg).status == CheckStatus.PASS
    cfg.dashboard_host = "0.0.0.0"
    assert doc._check_sendblue_listener(cfg).status == CheckStatus.WARNING
    cfg.dashboard_host = "127.0.0.1"
    assert doc._check_sendblue_listener(cfg).status == CheckStatus.PASS


async def test_request_paused_in_identity_cannot_enqueue_after_stop(channel, client):
    entered, release = asyncio.Event(), asyncio.Event()

    async def guest(*args):
        entered.set()
        await release.wait()
        return False

    channel._identity = MagicMock(has_config=True)
    channel._identity.is_guest = guest
    pending = asyncio.create_task(post(client, event()))
    await asyncio.wait_for(entered.wait(), 2)
    await channel.stop()
    release.set()
    assert (await pending).status_code == 503
    assert channel._queue.empty()
    assert not channel._seen


async def test_doctor_rejects_invalid_entry_even_with_wildcard(sendblue_settings):
    from pincer.security.doctor import CheckStatus, SecurityDoctor

    sendblue_settings.sendblue_enabled = True
    sendblue_settings.sendblue_allow_from = ["*", "invalid"]
    assert SecurityDoctor()._check_sendblue_access(sendblue_settings).status == CheckStatus.CRITICAL


async def test_interrupted_startup_stops_turns_before_api_teardown(channel, client):
    from types import SimpleNamespace

    from pincer.cli.run import _Shutdown, _stop_api_server, _stop_channels_on_shutdown

    entered, cancelled = asyncio.Event(), asyncio.Event()
    order = []

    async def handler(message):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
            order.append("turn cancelled")

    channel._handler = handler
    await post(client, event())
    await asyncio.wait_for(entered.wait(), 2)

    class Server:
        should_exit = False

    server = Server()

    async def api_lifespan():
        while not server.should_exit:
            await asyncio.sleep(0)
        assert cancelled.is_set()
        assert not channel.running
        order.append("API dependencies closed")

    api_task = asyncio.create_task(api_lifespan())
    shutdown = _Shutdown()
    _stop_channels_on_shutdown(shutdown, SimpleNamespace(channels={"sendblue": channel}))
    shutdown.on_stop("API server", lambda: _stop_api_server(server, api_task, channel))
    # No final "startup complete" callback: a tunnel/setup await was cancelled.
    try:
        await shutdown.run()
        assert not shutdown.failed
        assert order == ["turn cancelled", "API dependencies closed"]
    finally:
        server.should_exit = True
        await asyncio.gather(api_task, return_exceptions=True)


async def test_inflight_tasks_count_toward_bounded_admission(channel, client):
    release = asyncio.Event()
    channel._settings.sendblue_allow_from = ["*"]

    async def handler(message):
        await release.wait()
        return ""

    channel._handler = handler
    try:
        for n in range(128):
            assert (await post(client, event(message_handle=f"bounded-{n}"))).status_code == 200
        assert (await post(client, event(message_handle="overflow"))).status_code == 503
        assert "overflow" not in channel._seen
    finally:
        release.set()
        await asyncio.wait_for(channel._queue.join(), 3)
    assert (await post(client, event(message_handle="overflow"))).status_code == 200
    await channel._queue.join()
