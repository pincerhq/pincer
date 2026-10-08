"""Sendblue's public HTTP boundary, real agent loop, and provider acceptance contract."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aiohttp import ClientSession, web
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
    # An ephemeral listener is only a test override (production settings require >0).
    settings.sendblue_webhook_port = 0
    return settings


@pytest.fixture
async def channel(sendblue_settings):
    instance = SendblueChannel(sendblue_settings)
    await instance.start(AsyncMock(return_value=""))
    yield instance
    await instance.stop()
    from pincer.db.engine import dispose_engines

    await dispose_engines()


@pytest.fixture
async def client(channel):
    port = channel._runner.addresses[0][1]
    async with ClientSession(base_url=f"http://127.0.0.1:{port}") as client:
        yield client


async def post(client, payload, secret=SECRET):
    return await client.post("/webhooks/sendblue", json=payload, headers={"sb-signing-secret": secret})


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
        assert (await post(client, event())).status == 200
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
        assert (await post(client, event())).status == 200
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
    assert (await post(client, event(**change))).status == status
    await channel._queue.join()
    channel._handler.assert_not_called()


async def test_authentication_and_bounded_payload(channel, client):
    assert (await post(client, event(), secret="wrong")).status == 401
    assert (await post(client, event(), secret="\u00e9")).status == 401
    assert (await post(client, [])).status == 400
    response = await client.post("/webhooks/sendblue", data="{", headers={"sb-signing-secret": SECRET})
    assert response.status == 400
    assert (await post(client, event(content="x" * 70000))).status == 413
    channel._handler.assert_not_called()


async def test_queue_pressure_does_not_poison_retry(channel, client):
    channel._worker.cancel()
    await asyncio.gather(channel._worker, return_exceptions=True)
    channel._worker = None
    for n in range(128):
        assert (await post(client, event(message_handle=f"m-{n}"))).status == 200
    assert (await post(client, event(message_handle="retry"))).status == 503
    channel._queue.get_nowait()
    channel._queue.task_done()
    assert (await post(client, event(message_handle="retry"))).status == 200


async def test_media_is_not_downloaded(channel, client):
    assert (await post(client, event(content=None, media_url="http://169.254.169.254/secret"))).status == 200
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
    assert (await post(client, event())).status == 200
    await channel._queue.join()
    assert (await post(client, event())).status == 200
    await channel._queue.join()
    channel._handler.assert_awaited_once()


async def test_invalid_allowlist_fails_before_opening_listener(sendblue_settings):
    sendblue_settings.sendblue_allow_from = ["not-a-phone"]
    channel = SendblueChannel(sendblue_settings)
    with pytest.raises(ValueError, match="allowlist entries"):
        await channel.start(AsyncMock())
    assert channel._client is None
    assert channel._runner is None
    assert channel._worker is None


@pytest.mark.parametrize("error", [OSError("port unavailable"), asyncio.CancelledError()])
async def test_startup_failure_closes_partial_resources(sendblue_settings, monkeypatch, error):
    channel = SendblueChannel(sendblue_settings)
    clients = []

    async def fail_start(site):
        clients.append(channel._client)
        raise error

    monkeypatch.setattr(web.TCPSite, "start", fail_start)
    with pytest.raises(type(error)):
        await channel.start(AsyncMock())
    assert clients[0].is_closed
    assert channel._client is None
    assert channel._runner is None
    assert channel._worker is None
    await channel.stop()  # Cleanup remains safe after a failed start.


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
    assert (await post(client, event())).status == 200
    await channel._queue.join()
    channel._handler.assert_not_called()
    assert not channel._seen
    assert (await post(client, event(from_number=known))).status == 200
    await channel._queue.join()
    channel._handler.assert_awaited_once()
    assert channel._handler.call_args.args[0].user_id == known


@pytest.mark.parametrize("content", [None, "", " \n\t"])
async def test_empty_messages_are_not_queued_or_remembered(channel, client, content):
    assert (await post(client, event(content=content))).status == 200
    await channel._queue.join()
    channel._handler.assert_not_called()
    assert not channel._seen


async def test_duplicate_cache_evicts_oldest_and_retains_recent(channel, client):
    channel._seen.update((f"old-{n}", None) for n in range(4096))
    assert (await post(client, event())).status == 200
    await channel._queue.join()
    assert len(channel._seen) == 4096
    assert "old-0" not in channel._seen
    assert (await post(client, event(message_handle="old-4095"))).status == 200
    await channel._queue.join()
    channel._handler.assert_awaited_once()
    assert (await post(client, event(message_handle="old-0"))).status == 200
    await channel._queue.join()
    assert channel._handler.await_count == 2
    assert len(channel._seen) == 4096


async def test_worker_drains_uninitialized_handler_and_recovers(channel, client, caplog):
    channel._handler = None
    assert (await post(client, event())).status == 200
    await asyncio.wait_for(channel._queue.join(), 2)
    assert "fixture-inbound-1; no automatic replay" in caplog.text
    assert not channel._worker.done()
    channel._handler = AsyncMock(return_value="")
    assert (await post(client, event(message_handle="next"))).status == 200
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
    [(True, None), (False, None), (True, ValueError), (True, OSError), (True, ModuleNotFoundError)],
)
async def test_cli_sendblue_startup_registers_live_reply_and_proactive_routes(
    sendblue_settings, mock_llm, session_manager, cost_tracker, tool_registry, monkeypatch, enabled, start_error, capsys
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
    telegram = None
    occupied_listener = None
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
        elif start_error is OSError:
            occupied_listener = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0)
            settings.sendblue_webhook_port = occupied_listener.sockets[0].getsockname()[1]
        else:
            import builtins

            original_import = builtins.__import__

            def without_aiohttp(name, *args, **kwargs):
                if name == "aiohttp":
                    raise ModuleNotFoundError("sensitive fixture detail")
                return original_import(name, *args, **kwargs)

            monkeypatch.setattr(builtins, "__import__", without_aiohttp)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
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
            await cli._run_agent(settings)
            assert maps == [{}]
            assert not routers
            mock_llm.complete.assert_not_called()
            return

        with pytest.raises(ChannelsStarted):
            await cli._run_agent(settings)
        if start_error:
            assert maps == [{"telegram": telegram}]
            assert routers[0].channels == {ChannelType.TELEGRAM: telegram}
            telegram.start.assert_awaited_once()
            telegram.stop.assert_not_awaited()
            output = capsys.readouterr().out
            assert f"Sendblue failed to start ({start_error.__name__})" in output
            assert "sensitive fixture detail" not in output
            return
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
            port = channel._runner.addresses[0][1]
            async with ClientSession(base_url=f"http://127.0.0.1:{port}") as client:
                assert (await post(client, event())).status == 200
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
            async with ClientSession(base_url=f"http://127.0.0.1:{port}") as client:
                assert (await post(client, event(message_handle="approval", content="Run risky"))).status == 200
                await asyncio.wait_for(channel._queue.join(), 10)
            assert not executed
            session = await session_manager.get_or_create(canonical, "sendblue")
            assert any(
                m.tool_call_id == "approval-check" and "declined" in (m.content or "").lower() for m in session.messages
            )
            assert "Action declined" in received[-1]["content"]
    finally:
        if occupied_listener is not None:
            occupied_listener.close()
            await occupied_listener.wait_closed()
        for channel_map in maps:
            for channel in channel_map.values():
                await channel.stop()
        await dispose_engines()


async def test_invalid_header_bytes_fail_authentication(channel):
    port = channel._runner.addresses[0][1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(
            b"POST /webhooks/sendblue HTTP/1.1\r\nHost: localhost\r\n"
            b"sb-signing-secret: \xff\xfe\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        assert b" 401 " in await asyncio.wait_for(reader.readline(), 2)
        channel._handler.assert_not_called()
    finally:
        writer.close()
        await writer.wait_closed()


async def test_unknown_charset_cannot_raise_server_error(channel, client):
    headers = {"sb-signing-secret": SECRET, "Content-Type": "application/json; charset=nope"}
    response = await client.post("/webhooks/sendblue", data=b"{", headers=headers)
    assert response.status == 400
    channel._handler.assert_not_called()
    response = await client.post("/webhooks/sendblue", data=json.dumps(event()).encode(), headers=headers)
    assert response.status == 200
    await channel._queue.join()
    channel._handler.assert_awaited_once()


async def test_empty_allowlist_starts_but_denies_all(sendblue_settings):
    sendblue_settings.sendblue_allow_from = []
    channel = SendblueChannel(sendblue_settings)
    handler = AsyncMock(return_value="")
    try:
        await channel.start(handler)
        port = channel._runner.addresses[0][1]
        async with ClientSession(base_url=f"http://127.0.0.1:{port}") as client:
            assert (await post(client, event())).status == 200
        await channel._queue.join()
        handler.assert_not_called()
        assert not channel._seen
        with pytest.raises(ValueError, match="allowlist"):
            await channel.send(SENDER, "blocked")
    finally:
        await channel.stop()
