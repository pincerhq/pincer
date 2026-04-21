"""Tests for WhatsApp channel."""

import asyncio
import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest

from pincer.channels.base import ChannelType


def _ensure_neonize_mocks():
    """Pre-populate sys.modules with neonize stubs so whatsapp.py can import."""
    mods = {}
    for name in (
        "neonize",
        "neonize.aioze",
        "neonize.aioze.client",
        "neonize.aioze.events",
        "neonize.proto",
        "neonize.proto.waE2E",
        "neonize.proto.waE2E.WAWebProtobufsE2E_pb2",
        "neonize.utils",
        "neonize.client",
        "magic",
    ):
        if name not in sys.modules:
            mod = ModuleType(name)
            sys.modules[name] = mod
            mods[name] = mod

    neonize_client = sys.modules["neonize.aioze.client"]
    neonize_client.NewAClient = MagicMock  # type: ignore[attr-defined]

    neonize_pkg = sys.modules["neonize"]
    neonize_pkg.__version__ = "0.3.16.post0"  # type: ignore[attr-defined]

    neonize_events = sys.modules["neonize.aioze.events"]
    for ev in (
        "ConnectedEv",
        "MessageEv",
        "PairStatusEv",
        "QREv",
        "ConnectFailureEv",
        "ClientOutdatedEv",
        "LoggedOutEv",
        "StreamErrorEv",
    ):
        setattr(neonize_events, ev, type(ev, (), {}))
    mock_loop = MagicMock()
    mock_loop.is_running.return_value = False
    neonize_events.event_global_loop = mock_loop  # type: ignore[attr-defined]

    neonize_proto = sys.modules["neonize.proto.waE2E.WAWebProtobufsE2E_pb2"]
    neonize_proto.Message = MagicMock  # type: ignore[attr-defined]

    neonize_utils = sys.modules["neonize.utils"]
    neonize_utils.build_jid = MagicMock()  # type: ignore[attr-defined]
    neonize_utils.Jid2String = MagicMock(return_value="1234@s.whatsapp.net")  # type: ignore[attr-defined]
    neonize_utils.log = MagicMock()  # type: ignore[attr-defined]

    return mods


_ensure_neonize_mocks()

# Force reimport with mocked neonize
if "pincer.channels.whatsapp" in sys.modules:
    del sys.modules["pincer.channels.whatsapp"]

from pincer.channels.whatsapp import WhatsAppChannel  # noqa: E402


@pytest.fixture
def wa_channel():
    settings = MagicMock()
    settings.whatsapp_dm_allowlist = "+491234567890,+491111111111"
    settings.whatsapp_self_chat_only = False
    settings.whatsapp_group_trigger = "pincer"
    settings.data_dir = "/tmp/test"
    settings.openai_api_key.get_secret_value.return_value = "test-key"
    yield WhatsAppChannel(settings)


class TestWhatsAppChannel:
    def test_channel_type(self, wa_channel):
        assert wa_channel.channel_type == ChannelType.WHATSAPP

    def test_name(self, wa_channel):
        assert wa_channel.name == "whatsapp"

    def test_allowlist_parsing(self, wa_channel):
        assert "491234567890" in wa_channel._dm_allowlist
        assert "491111111111" in wa_channel._dm_allowlist
        assert len(wa_channel._dm_allowlist) == 2

    def test_empty_allowlist(self):
        settings = MagicMock()
        settings.whatsapp_dm_allowlist = ""
        settings.whatsapp_self_chat_only = True
        settings.whatsapp_group_trigger = "pincer"
        settings.data_dir = "/tmp/test"
        ch = WhatsAppChannel(settings)
        assert len(ch._dm_allowlist) == 0

    def test_mention_by_trigger_word(self, wa_channel):
        wa_channel._own_jid = "491234567890"
        msg = MagicMock()
        msg.conversation = "hey @pincer what's up?"
        msg.HasField = MagicMock(return_value=False)
        assert wa_channel._is_mentioned_in_group(msg, MagicMock()) is True

    def test_mention_by_jid(self, wa_channel):
        wa_channel._own_jid = "491234567890"
        msg = MagicMock()
        msg.conversation = "hey 491234567890 what's up?"
        msg.HasField = MagicMock(return_value=False)
        assert wa_channel._is_mentioned_in_group(msg, MagicMock()) is True

    def test_no_mention(self, wa_channel):
        wa_channel._own_jid = "491234567890"
        msg = MagicMock()
        msg.conversation = "hey everyone, lunch?"
        msg.HasField = MagicMock(return_value=False)
        assert wa_channel._is_mentioned_in_group(msg, MagicMock()) is False

    def test_trigger_case_insensitive(self, wa_channel):
        wa_channel._own_jid = "491234567890"
        msg = MagicMock()
        msg.conversation = "Hey PINCER, do this"
        msg.HasField = MagicMock(return_value=False)
        assert wa_channel._is_mentioned_in_group(msg, MagicMock()) is True


class TestWhatsAppMediaAndApproval:
    """Regression tests: tools using media delivery and approval on WhatsApp."""

    @pytest.mark.asyncio
    async def test_send_chunks_long_messages(self, wa_channel):
        """`send()` must split messages longer than the 4096-char limit."""
        wa_channel._client = MagicMock()
        wa_channel._client.send_message = AsyncMock()

        long_text = ("paragraph " * 600).strip()
        assert len(long_text) > 4096

        await wa_channel.send("491234567890", long_text)

        assert wa_channel._client.send_message.await_count >= 2
        for call in wa_channel._client.send_message.await_args_list:
            assert len(call.args[1]) <= 4096

    @pytest.mark.asyncio
    async def test_send_photo_uses_neonize_image(self, wa_channel):
        wa_channel._client = MagicMock()
        wa_channel._client.send_image = AsyncMock()

        await wa_channel.send_photo("491234567890", "https://example.com/cat.png", caption="cat")

        wa_channel._client.send_image.assert_awaited_once()
        kwargs = wa_channel._client.send_image.await_args.kwargs
        assert kwargs["caption"] == "cat"

    @pytest.mark.asyncio
    async def test_send_photo_from_bytes(self, wa_channel):
        wa_channel._client = MagicMock()
        wa_channel._client.send_image = AsyncMock()

        await wa_channel.send_photo_from_bytes(
            "491234567890",
            b"PNG_DATA",
            "image/png",
            "hi",
        )

        wa_channel._client.send_image.assert_awaited_once()
        args = wa_channel._client.send_image.await_args.args
        assert args[1] == b"PNG_DATA"

    @pytest.mark.asyncio
    async def test_send_file_uses_neonize_document(self, wa_channel, tmp_path):
        wa_channel._client = MagicMock()
        wa_channel._client.send_document = AsyncMock()

        f = tmp_path / "report.pdf"
        f.write_bytes(b"%PDF-1.4 fake")

        await wa_channel.send_file("491234567890", str(f), caption="Q4")

        wa_channel._client.send_document.assert_awaited_once()
        kwargs = wa_channel._client.send_document.await_args.kwargs
        assert kwargs["filename"] == "report.pdf"
        assert kwargs["caption"] == "Q4"

    @pytest.mark.asyncio
    async def test_send_animation_uses_send_video_with_gifplayback(self, wa_channel):
        wa_channel._client = MagicMock()
        wa_channel._client.send_video = AsyncMock()

        await wa_channel.send_animation("491234567890", "https://example.com/a.gif", "lol")

        wa_channel._client.send_video.assert_awaited_once()
        kwargs = wa_channel._client.send_video.await_args.kwargs
        assert kwargs["gifplayback"] is True
        assert kwargs["is_gif"] is True

    @pytest.mark.asyncio
    async def test_request_approval_yes(self, wa_channel):
        wa_channel._client = MagicMock()
        wa_channel._client.send_message = AsyncMock()

        async def resolve_later():
            await asyncio.sleep(0.01)
            fut = wa_channel._pending_approvals.get("491234567890")
            assert fut is not None
            fut.set_result(True)

        resolver = asyncio.create_task(resolve_later())
        result = await wa_channel.request_approval(
            "491234567890", "shell_exec", {"cmd": "ls"}
        )
        await resolver
        assert result is True

    @pytest.mark.asyncio
    async def test_request_approval_no(self, wa_channel):
        wa_channel._client = MagicMock()
        wa_channel._client.send_message = AsyncMock()

        async def deny_later():
            await asyncio.sleep(0.01)
            fut = wa_channel._pending_approvals.get("491234567890")
            fut.set_result(False)

        t = asyncio.create_task(deny_later())
        result = await wa_channel.request_approval(
            "491234567890", "shell_exec", {"cmd": "rm -rf"}
        )
        await t
        assert result is False

    @pytest.mark.asyncio
    async def test_request_approval_denies_when_send_fails(self, wa_channel, caplog):
        """Undeliverable approval prompts (e.g. no LID) should deny cleanly."""
        import logging

        wa_channel._client = MagicMock()
        wa_channel._client.send_message = AsyncMock(
            side_effect=RuntimeError("no LID found for 207855026221128@s.whatsapp.net")
        )

        with caplog.at_level(logging.WARNING, logger="pincer.channels.whatsapp"):
            result = await wa_channel.request_approval(
                "491234567890", "google__create_doc", {"title": "x"}
            )

        assert result is False
        assert "491234567890" not in wa_channel._pending_approvals
        assert any("undeliverable" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_request_input_returns_fallback_when_send_fails(self, wa_channel):
        """Undeliverable input prompts should return a user-visible message, not raise."""
        wa_channel._client = MagicMock()
        wa_channel._client.send_message = AsyncMock(
            side_effect=RuntimeError("no LID found")
        )

        result = await wa_channel.request_input("491234567890", "What's your name?")

        assert result == "[Could not deliver question to user]"
        assert "491234567890" not in wa_channel._pending_inputs

    @pytest.mark.asyncio
    async def test_send_uses_lid_server_after_lid_inbound(self, wa_channel, monkeypatch):
        """After an @lid message arrives, `send()` must route to @lid, not default."""
        # Other test files in this suite del+reimport `pincer.channels.whatsapp`,
        # so sys.modules[...] isn't necessarily the module our fixture's class
        # was defined in. Patch the class's actual globals dict instead.
        wa_globals = type(wa_channel)._jid_for.__globals__
        captured: list[tuple] = []

        def fake_build_jid(user, server=None):
            captured.append((user, server))
            return f"{user}@{server or 'default'}"

        monkeypatch.setitem(wa_globals, "build_jid", fake_build_jid)

        wa_channel._client = MagicMock()
        wa_channel._client.send_message = AsyncMock()
        wa_channel._reply_targets["207855026221128"] = ("207855026221128", "lid")

        await wa_channel.send("207855026221128", "hi")

        assert ("207855026221128", "lid") in captured

    @pytest.mark.asyncio
    async def test_send_honors_explicit_server_in_user_id(self, wa_channel, monkeypatch):
        """A user_id like '207855026221128@lid' must route to @lid."""
        wa_globals = type(wa_channel)._jid_for.__globals__
        captured: list[tuple] = []

        def fake_build_jid(user, server=None):
            captured.append((user, server))
            return f"{user}@{server or 'default'}"

        monkeypatch.setitem(wa_globals, "build_jid", fake_build_jid)

        wa_channel._client = MagicMock()
        wa_channel._client.send_message = AsyncMock()

        await wa_channel.send("207855026221128@lid", "hi")

        assert ("207855026221128", "lid") in captured

    @pytest.mark.asyncio
    async def test_send_defaults_to_bare_user_when_no_target_learned(
        self, wa_channel, monkeypatch
    ):
        """Unknown user_id with no '@' falls back to neonize default server."""
        wa_globals = type(wa_channel)._jid_for.__globals__
        captured: list[tuple] = []

        def fake_build_jid(user, server=None):
            captured.append((user, server))
            return f"{user}@default"

        monkeypatch.setitem(wa_globals, "build_jid", fake_build_jid)

        wa_channel._client = MagicMock()
        wa_channel._client.send_message = AsyncMock()

        await wa_channel.send("491234567890", "hi")

        assert ("491234567890", None) in captured

    def test_parse_yes_no(self, wa_channel):
        assert wa_channel._parse_yes_no("yes") is True
        assert wa_channel._parse_yes_no(" Approve ") is True
        assert wa_channel._parse_yes_no("no") is False
        assert wa_channel._parse_yes_no("DENY") is False
        assert wa_channel._parse_yes_no("maybe") is None
        assert wa_channel._parse_yes_no("") is None

    @pytest.mark.asyncio
    async def test_set_identity_resolver(self, wa_channel):
        resolver = MagicMock()
        resolver.resolve = AsyncMock(return_value="pincer_user_42")

        wa_channel.set_identity_resolver(resolver)
        assert wa_channel._identity is resolver

        result = await wa_channel._resolve_identity("491234567890")
        assert result == "pincer_user_42"
        resolver.resolve.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_resolve_identity_without_resolver_returns_empty(self, wa_channel):
        assert wa_channel._identity is None
        assert await wa_channel._resolve_identity("491234567890") == ""


class TestWhatsAppLoginFailure:
    """Login-failure handlers must surface actionable errors and unblock start()."""

    @pytest.mark.asyncio
    async def test_connect_failure_client_outdated_records_actionable_error(
        self, wa_channel
    ):
        event = MagicMock()
        event.Reason = 6  # CLIENT_OUTDATED
        await wa_channel._on_connect_failure(MagicMock(), event)

        assert wa_channel._login_error is not None
        assert wa_channel._login_error.startswith("client_outdated:")
        assert "neonize" in wa_channel._login_error
        assert wa_channel._connected.is_set()

    @pytest.mark.asyncio
    async def test_connect_failure_logged_out_maps_reason(self, wa_channel):
        event = MagicMock()
        event.Reason = 2  # LOGGED_OUT
        await wa_channel._on_connect_failure(MagicMock(), event)

        assert wa_channel._login_error.startswith("logged_out:")
        assert "re-pair" in wa_channel._login_error

    @pytest.mark.asyncio
    async def test_connect_failure_unknown_reason_is_generic(self, wa_channel):
        event = MagicMock()
        event.Reason = 99  # not in the remediation map
        await wa_channel._on_connect_failure(MagicMock(), event)

        assert wa_channel._login_error.startswith("connect_failure:")
        assert wa_channel._connected.is_set()

    @pytest.mark.asyncio
    async def test_client_outdated_handler_records_error(self, wa_channel):
        await wa_channel._on_client_outdated(MagicMock(), MagicMock())

        assert wa_channel._login_error.startswith("client_outdated:")
        assert wa_channel._connected.is_set()

    @pytest.mark.asyncio
    async def test_logged_out_handler_records_error(self, wa_channel):
        await wa_channel._on_logged_out(MagicMock(), MagicMock())

        assert wa_channel._login_error.startswith("logged_out:")
        assert wa_channel._connected.is_set()

    @pytest.mark.asyncio
    async def test_stream_error_logs_but_does_not_block_start(self, wa_channel):
        event = MagicMock()
        event.Code = "xml-not-well-formed"
        await wa_channel._on_stream_error(MagicMock(), event)

        # Stream errors are a runtime signal, not a login failure — start()
        # should still be able to wait for a real Connected event.
        assert wa_channel._login_error is None
        assert not wa_channel._connected.is_set()
