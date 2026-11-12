"""Tests for WhatsApp channel."""

import asyncio
import logging
import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qsl, urlsplit

import pytest
from pydantic import SecretStr

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
        "neonize.utils.enum",
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

    neonize_enum = sys.modules["neonize.utils.enum"]
    neonize_enum.ChatPresence = MagicMock()  # type: ignore[attr-defined]
    neonize_enum.ChatPresenceMedia = MagicMock()  # type: ignore[attr-defined]

    return mods


_ensure_neonize_mocks()

# Force reimport with mocked neonize
if "pincer.channels.whatsapp" in sys.modules:
    del sys.modules["pincer.channels.whatsapp"]

from pincer.channels.whatsapp import WhatsAppChannel  # noqa: E402


@pytest.fixture
def wa_channel():
    settings = MagicMock()
    settings.whatsapp_self_chat_only = False
    settings.whatsapp_group_trigger = "pincer"
    settings.whatsapp_group_trigger_on_mention = False
    settings.whatsapp_group_trigger_on_reply = True
    settings.data_dir = "/tmp/test"
    settings.whatsapp_session_db = SecretStr("")
    settings.whatsapp_jid = ""
    settings.openai_api_key.get_secret_value.return_value = "test-key"
    yield WhatsAppChannel(settings)


class TestWhatsAppChannel:
    def test_channel_type(self, wa_channel):
        assert wa_channel.channel_type == ChannelType.WHATSAPP

    def test_name(self, wa_channel):
        assert wa_channel.name == "whatsapp"

    def test_mention_by_trigger_word(self, wa_channel):
        wa_channel._own_jid = "491234567890"
        msg = MagicMock()
        msg.conversation = "hey @pincer what's up?"
        msg.HasField = MagicMock(return_value=False)
        assert wa_channel._is_addressed_in_group(msg) is True

    def test_mention_by_jid(self, wa_channel):
        wa_channel._settings.whatsapp_group_trigger_on_mention = True
        wa_channel._own_jid = "491234567890"
        msg = MagicMock()
        msg.conversation = "hey 491234567890 what's up?"
        msg.HasField = MagicMock(return_value=False)
        assert wa_channel._is_addressed_in_group(msg) is True

    def test_no_mention(self, wa_channel):
        wa_channel._own_jid = "491234567890"
        msg = MagicMock()
        msg.conversation = "hey everyone, lunch?"
        msg.HasField = MagicMock(return_value=False)
        assert wa_channel._is_addressed_in_group(msg) is False

    def test_trigger_case_insensitive(self, wa_channel):
        wa_channel._own_jid = "491234567890"
        msg = MagicMock()
        msg.conversation = "Hey PINCER, do this"
        msg.HasField = MagicMock(return_value=False)
        assert wa_channel._is_addressed_in_group(msg) is True

    def test_trigger_list_phrase(self, wa_channel):
        wa_channel._own_jid = "491234567890"
        wa_channel._settings.whatsapp_group_trigger = "@pincer, hey pincer"
        msg = MagicMock()
        msg.conversation = "hey\n pincer, do this"
        msg.HasField = MagicMock(return_value=False)
        assert wa_channel._is_addressed_in_group(msg) is True

    def test_jid_inside_longer_number_does_not_match(self, wa_channel):
        wa_channel._own_jid = "491234567890"
        msg = MagicMock()
        msg.conversation = "order 14912345678901 shipped"
        msg.HasField = MagicMock(return_value=False)
        assert wa_channel._is_addressed_in_group(msg) is False

    @staticmethod
    def _ext_msg_with_mentions(
        text: str, mentioned: list[str], participant: str = "", quoted_id: str = ""
    ) -> MagicMock:
        ext = MagicMock()
        ext.text = text
        ext.contextInfo.mentionedJID = mentioned
        ext.contextInfo.participant = participant
        ext.contextInfo.stanzaID = quoted_id
        ext.HasField = MagicMock(side_effect=lambda f: f == "contextInfo")
        msg = MagicMock()
        msg.extendedTextMessage = ext
        msg.HasField = MagicMock(side_effect=lambda f: f == "extendedTextMessage")
        return msg

    def test_mention_by_lid(self, wa_channel):
        wa_channel._settings.whatsapp_group_trigger_on_mention = True
        wa_channel._own_jid = "491234567890"
        wa_channel._own_lid = "207855026221128"
        msg = self._ext_msg_with_mentions("@207855026221128 help", ["207855026221128@lid"])
        assert wa_channel._is_addressed_in_group(msg) is True

    def test_mention_in_media_caption_context(self, wa_channel):
        wa_channel._settings.whatsapp_group_trigger_on_mention = True
        wa_channel._own_jid = "491234567890"
        wa_channel._settings.whatsapp_group_trigger = ""
        image = MagicMock()
        image.caption = "@Pincer what is this?"
        image.contextInfo.mentionedJID = ["491234567890@s.whatsapp.net"]
        image.HasField = MagicMock(side_effect=lambda f: f == "contextInfo")
        msg = MagicMock()
        msg.conversation = ""
        msg.imageMessage = image
        msg.HasField = MagicMock(side_effect=lambda f: f == "imageMessage")
        assert wa_channel._is_addressed_in_group(msg) is True

    def test_mentioned_jid_with_longer_number_does_not_match(self, wa_channel):
        wa_channel._own_jid = "491234567890"
        msg = self._ext_msg_with_mentions("@Bob hi", ["14912345678901@s.whatsapp.net"])
        assert wa_channel._is_addressed_in_group(msg) is False

    def test_mention_ignored_when_on_mention_disabled(self, wa_channel, caplog):
        wa_channel._own_jid = "491234567890"
        wa_channel._own_lid = "207855026221128"
        wa_channel._settings.whatsapp_group_trigger_on_mention = False
        msg = self._ext_msg_with_mentions("@207855026221128 help", ["207855026221128@lid"])
        with caplog.at_level(logging.DEBUG, logger="pincer.channels.whatsapp"):
            assert wa_channel._is_addressed_in_group(msg) is False
        assert "PINCER_WHATSAPP_GROUP_TRIGGER_ON_MENTION is off" in caplog.text

    def test_jid_in_text_ignored_when_on_mention_disabled(self, wa_channel):
        wa_channel._own_jid = "491234567890"
        wa_channel._settings.whatsapp_group_trigger_on_mention = False
        msg = MagicMock()
        msg.conversation = "hey @491234567890 what's up?"
        msg.HasField = MagicMock(return_value=False)
        assert wa_channel._is_addressed_in_group(msg) is False

    def test_trigger_works_when_on_mention_disabled(self, wa_channel):
        wa_channel._own_jid = "491234567890"
        wa_channel._settings.whatsapp_group_trigger_on_mention = False
        msg = self._ext_msg_with_mentions("@Bob ask pincer", ["14912345678901@s.whatsapp.net"])
        assert wa_channel._is_addressed_in_group(msg) is True

    def test_mention_without_trigger_list(self, wa_channel):
        wa_channel._settings.whatsapp_group_trigger_on_mention = True
        wa_channel._own_jid = "491234567890"
        wa_channel._own_lid = "207855026221128"
        wa_channel._settings.whatsapp_group_trigger = ""
        msg = self._ext_msg_with_mentions("@207855026221128 help", ["207855026221128@lid"])
        assert wa_channel._is_addressed_in_group(msg) is True

    @staticmethod
    def _sent(channel, message_id: str) -> None:
        channel._remember_pincer_message(MagicMock(ID=message_id))

    def test_reply_to_pincer_message(self, wa_channel):
        wa_channel._own_jid = "491234567890"
        self._sent(wa_channel, "3EB0PINCER")
        msg = self._ext_msg_with_mentions("what about tomorrow?", [], "491234567890@s.whatsapp.net", "3EB0PINCER")
        assert wa_channel._is_addressed_in_group(msg) is True

    def test_reply_to_pincer_message_quoted_by_lid(self, wa_channel):
        wa_channel._own_jid = "491234567890"
        wa_channel._own_lid = "207855026221128"
        self._sent(wa_channel, "3EB0PINCER")
        msg = self._ext_msg_with_mentions("and then?", [], "207855026221128:12@lid", "3EB0PINCER")
        assert wa_channel._is_addressed_in_group(msg) is True

    def test_reply_to_the_persons_own_message_ignored(self, wa_channel):
        """Shared number: a member replying to the person's own message is not for Pincer."""
        wa_channel._own_jid = "491234567890"
        self._sent(wa_channel, "3EB0PINCER")
        msg = self._ext_msg_with_mentions("bring wine", [], "491234567890@s.whatsapp.net", "3EB0PERSON")
        assert wa_channel._is_addressed_in_group(msg) is False

    def test_reply_to_someone_else(self, wa_channel):
        wa_channel._own_jid = "491234567890"
        self._sent(wa_channel, "3EB0PINCER")
        msg = self._ext_msg_with_mentions("agreed", [], "14912345678901@s.whatsapp.net", "3EB0OTHER")
        assert wa_channel._is_addressed_in_group(msg) is False

    def test_reply_ignored_when_on_reply_disabled(self, wa_channel, caplog):
        wa_channel._own_jid = "491234567890"
        wa_channel._settings.whatsapp_group_trigger_on_reply = False
        self._sent(wa_channel, "3EB0PINCER")
        msg = self._ext_msg_with_mentions("what about tomorrow?", [], "491234567890@s.whatsapp.net", "3EB0PINCER")
        with caplog.at_level(logging.DEBUG, logger="pincer.channels.whatsapp"):
            assert wa_channel._is_addressed_in_group(msg) is False
        assert "PINCER_WHATSAPP_GROUP_TRIGGER_ON_REPLY is off" in caplog.text

    def test_trigger_works_when_on_reply_disabled(self, wa_channel):
        wa_channel._own_jid = "491234567890"
        wa_channel._settings.whatsapp_group_trigger_on_reply = False
        self._sent(wa_channel, "3EB0PINCER")
        msg = self._ext_msg_with_mentions("pincer, and tomorrow?", [], "491234567890@s.whatsapp.net", "3EB0PINCER")
        assert wa_channel._is_addressed_in_group(msg) is True

    def test_reply_from_own_number_ignored(self, wa_channel, caplog):
        """Shared number: the person quoting Pincer's answer is a comment, not a prompt."""
        wa_channel._own_jid = "491234567890"
        self._sent(wa_channel, "3EB0PINCER")
        msg = self._ext_msg_with_mentions("ha, exactly", [], "491234567890@s.whatsapp.net", "3EB0PINCER")
        with caplog.at_level(logging.DEBUG, logger="pincer.channels.whatsapp"):
            assert wa_channel._is_addressed_in_group(msg, from_me=True) is False
        assert "reply from our own number ignored" in caplog.text

    def test_trigger_from_own_number_in_reply(self, wa_channel):
        wa_channel._own_jid = "491234567890"
        self._sent(wa_channel, "3EB0PINCER")
        msg = self._ext_msg_with_mentions("pincer, and tomorrow?", [], "491234567890@s.whatsapp.net", "3EB0PINCER")
        assert wa_channel._is_addressed_in_group(msg, from_me=True) is True

    def test_reply_in_image_caption(self, wa_channel):
        wa_channel._own_jid = "491234567890"
        self._sent(wa_channel, "3EB0PINCER")
        image = MagicMock()
        image.caption = "like this one?"
        image.contextInfo.mentionedJID = []
        image.contextInfo.participant = "491234567890@s.whatsapp.net"
        image.contextInfo.stanzaID = "3EB0PINCER"
        image.HasField = MagicMock(side_effect=lambda f: f == "contextInfo")
        msg = MagicMock()
        msg.conversation = ""
        msg.imageMessage = image
        msg.HasField = MagicMock(side_effect=lambda f: f == "imageMessage")
        assert wa_channel._is_addressed_in_group(msg) is True

    def test_remembered_pincer_messages_are_bounded(self, wa_channel):
        wa_channel._max_pincer_message_ids = 2
        for message_id in ("A", "B", "C"):
            self._sent(wa_channel, message_id)
        self._sent(wa_channel, "C")
        assert wa_channel._pincer_message_ids == {"B", "C"}
        assert list(wa_channel._pincer_message_ids_order) == ["B", "C"]

    @pytest.mark.asyncio
    async def test_sends_remember_their_message_ids(self, wa_channel):
        client = MagicMock()
        client.send_message = AsyncMock(side_effect=[MagicMock(ID="T1"), MagicMock(ID="T2")])
        client.send_image = AsyncMock(return_value=MagicMock(ID="I1"))
        wa_channel._client = client
        wa_channel._jid_for = AsyncMock(return_value="group@g.us")
        await wa_channel.send("120363000000000000", "hello")
        await wa_channel._send_tracked("120363000000000000", "status")
        await wa_channel.send_photo("120363000000000000", "https://example.com/a.png")
        assert wa_channel._pincer_message_ids == {"T1", "T2", "I1"}

    @pytest.mark.asyncio
    async def test_on_connected_seeds_own_lid(self, wa_channel):
        me = MagicMock()
        me.JID.User = "491234567890"
        me.HasField = MagicMock(side_effect=lambda f: f == "LID")
        me.LID.User = "207855026221128"
        client = MagicMock()
        client.get_me = AsyncMock(return_value=me)
        await wa_channel._on_connected(client, MagicMock())
        assert wa_channel._own_lid == "207855026221128"

    @pytest.mark.parametrize(
        ("field", "expected"),
        [
            ("imageMessage", True),
            ("documentMessage", True),
            # No content path handles video, so its caption must not pass the gate.
            ("videoMessage", False),
        ],
    )
    def test_trigger_in_media_caption(self, wa_channel, field, expected):
        wa_channel._own_jid = "491234567890"
        media = MagicMock()
        media.caption = "pincer, what is this?"
        media.HasField = MagicMock(return_value=False)
        msg = MagicMock()
        msg.conversation = ""
        setattr(msg, field, media)
        msg.HasField = MagicMock(side_effect=lambda f: f == field)
        assert wa_channel._is_addressed_in_group(msg) is expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("caption", "expected_text"),
        [
            ("pincer translate this", "pincer translate this\n\n[Document: report.pdf]"),
            ("", "[Document: report.pdf]"),
        ],
    )
    async def test_document_caption_reaches_agent(self, wa_channel, caption, expected_text):
        msg = MagicMock()
        msg.conversation = ""
        msg.documentMessage.mimetype = "application/pdf"
        msg.documentMessage.fileName = "report.pdf"
        msg.documentMessage.caption = caption
        msg.HasField = MagicMock(side_effect=lambda f: f == "documentMessage")
        client = MagicMock()
        client.download_any = AsyncMock(return_value=b"%PDF")
        incoming = await wa_channel._extract_message(client, MagicMock(), "491111111111", "chat", msg)
        assert incoming is not None
        assert incoming.text == expected_text
        assert incoming.files == [(b"%PDF", "application/pdf", "report.pdf")]

    def test_trigger_partial_word_does_not_match(self, wa_channel):
        wa_channel._own_jid = "491234567890"
        msg = MagicMock()
        msg.conversation = "pincers are cool"
        msg.HasField = MagicMock(return_value=False)
        assert wa_channel._is_addressed_in_group(msg) is False


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
            pending = wa_channel._pending_approvals.get("491234567890")
            assert pending is not None
            pending.future.set_result(True)

        resolver = asyncio.create_task(resolve_later())
        result = await wa_channel.request_approval("491234567890", "shell_exec", {"cmd": "ls"})
        await resolver
        assert result is True

    @pytest.mark.asyncio
    async def test_request_approval_no(self, wa_channel):
        wa_channel._client = MagicMock()
        wa_channel._client.send_message = AsyncMock()

        async def deny_later():
            await asyncio.sleep(0.01)
            wa_channel._pending_approvals["491234567890"].future.set_result(False)

        t = asyncio.create_task(deny_later())
        result = await wa_channel.request_approval("491234567890", "shell_exec", {"cmd": "rm -rf"})
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
            result = await wa_channel.request_approval("491234567890", "google__create_doc", {"title": "x"})

        assert result is False
        assert "491234567890" not in wa_channel._pending_approvals
        assert any("undeliverable" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_request_input_returns_fallback_when_send_fails(self, wa_channel):
        """Undeliverable input prompts should return a user-visible message, not raise."""
        wa_channel._client = MagicMock()
        wa_channel._client.send_message = AsyncMock(side_effect=RuntimeError("no LID found"))

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
    async def test_send_defaults_to_bare_user_when_no_target_learned(self, wa_channel, monkeypatch):
        """Unknown user_id with no '@' falls back to neonize default server."""
        wa_globals = type(wa_channel)._jid_for.__globals__
        captured: list[tuple] = []

        def fake_build_jid(user, server=None):
            captured.append((user, server))
            return f"{user}@default"

        monkeypatch.setitem(wa_globals, "build_jid", fake_build_jid)

        wa_channel._client = MagicMock()
        wa_channel._client.send_message = AsyncMock()
        # whatsmeow's store doesn't know it as a LID.
        wa_channel._client.get_pn_from_lid = AsyncMock(return_value=MagicMock(User=""))

        await wa_channel.send("491234567890", "hi")

        assert ("491234567890", None) in captured

    def test_parse_yes_no(self, wa_channel):
        assert wa_channel._parse_yes_no("yes") is True
        assert wa_channel._parse_yes_no(" Approve ") is True
        assert wa_channel._parse_yes_no("no") is False
        assert wa_channel._parse_yes_no("DENY") is False
        assert wa_channel._parse_yes_no("maybe") is None
        assert wa_channel._parse_yes_no("") is None

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Yes!", True),
            ("yes please", True),
            ("*no*", False),
            ("pincer yes", True),
            ("Pincer, no.", False),
            ("@491622549781 yes", True),
            # A mention of someone else addresses them, not us.
            ("@4915100000000 yes", None),
            ("@491622549781 @4915100000000 yes", None),
            ("491622549781 ok", True),
            ("👍🏽", True),
            ("так", True),
            ("Ja", True),
            ("нет", False),
            ("nein!", False),
            ("pincer", None),
            ("maybe yes", None),
            ("@4915100000000", None),
        ],
    )
    def test_parse_yes_no_lenient(self, wa_channel, text, expected):
        wa_channel._own_jid = "491622549781"
        assert wa_channel._parse_yes_no(text) is expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("pincer yes go ahead now", True),
            ("@491622549781 yes, go ahead and send it", True),
            ("yes go ahead please", None),
            ("pincer yes but use the other address", None),
        ],
    )
    def test_parse_yes_no_word_cap_skipped_when_addressed(self, wa_channel, text, expected):
        wa_channel._own_jid = "491622549781"
        wa_channel._settings.whatsapp_group_trigger = "pincer"
        assert wa_channel._parse_yes_no(text, max_words=3) is expected

    def test_reaction_sets_ignore_variation_selectors_and_skin_tones(self, wa_channel):
        from pincer.channels.approval_text import norm_emoji

        assert norm_emoji("❤️") in wa_channel._REACTION_APPROVE
        assert norm_emoji("👍🏻") in wa_channel._REACTION_APPROVE
        assert norm_emoji("👎🏿") in wa_channel._REACTION_DENY


class TestWhatsAppLoginFailure:
    """Login-failure handlers must surface actionable errors and unblock start()."""

    @pytest.mark.asyncio
    async def test_connect_failure_client_outdated_records_actionable_error(self, wa_channel):
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


class TestResolveInternalUserId:
    @pytest.fixture(autouse=True)
    def _jid_module(self, monkeypatch):
        jid_mod = ModuleType("neonize.utils.jid")
        jid_mod.build_jid = MagicMock()  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "neonize.utils.jid", jid_mod)

    @pytest.mark.asyncio
    async def test_known_phone_resolves_to_lid(self, wa_channel):
        wa_channel._client = MagicMock()
        wa_channel._client.get_lid_from_pn = AsyncMock(return_value=MagicMock(User="123456789012345"))

        assert await wa_channel.resolve_internal_user_id("+491622549781") == "123456789012345"

    @pytest.mark.asyncio
    async def test_unknown_phone_keeps_the_phone_number(self, wa_channel):
        # whatsmeow returns an empty JID, not an error, for a contact whose
        # LID it has not learned yet.
        wa_channel._client = MagicMock()
        wa_channel._client.get_lid_from_pn = AsyncMock(return_value=MagicMock(User=""))

        assert await wa_channel.resolve_internal_user_id("+491622549781") == "491622549781"

    @pytest.mark.asyncio
    async def test_failed_lookup_raises(self, wa_channel):
        # The identity layer leaves a pair out when its lookup fails; returning
        # the phone here would hide the failure from it.
        wa_channel._client = MagicMock()
        wa_channel._client.get_lid_from_pn = AsyncMock(side_effect=RuntimeError("not connected"))

        with pytest.raises(RuntimeError):
            await wa_channel.resolve_internal_user_id("+491622549781")


# ── Session store + account selection ───────────────────────────────


_PG_URL = "postgres://pincer_wa:hunter2secret@db:5432/pincer?sslmode=disable&search_path=whatsapp"


def _make_device_db(path, jids):
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE whatsmeow_device (jid TEXT PRIMARY KEY)")
    conn.executemany("INSERT INTO whatsmeow_device VALUES (?)", [(j,) for j in jids])
    conn.commit()
    conn.close()


class TestStoredDeviceJid:
    def test_hit_matches_user_part(self, tmp_path):
        from pincer.channels.whatsapp_store import stored_device_jid

        db = tmp_path / "pincer-wa.db"
        _make_device_db(db, ["491111111111:3@s.whatsapp.net", "492222222222:12@s.whatsapp.net"])
        assert stored_device_jid(str(db), "492222222222") == "492222222222:12@s.whatsapp.net"

    def test_miss(self, tmp_path):
        from pincer.channels.whatsapp_store import stored_device_jid

        db = tmp_path / "pincer-wa.db"
        _make_device_db(db, ["491111111111:3@s.whatsapp.net"])
        assert stored_device_jid(str(db), "4922") is None

    def test_missing_file_means_no_devices(self, tmp_path):
        from pincer.channels.whatsapp_store import stored_device_jid

        db = tmp_path / "absent.db"
        assert stored_device_jid(str(db), "491111111111") is None
        assert not db.exists()  # read-only: never created

    def test_missing_table_means_no_devices(self, tmp_path):
        import sqlite3

        from pincer.channels.whatsapp_store import stored_device_jid

        db = tmp_path / "empty.db"
        sqlite3.connect(db).close()
        assert stored_device_jid(str(db), "491111111111") is None

    def test_libpq_conninfo_and_describe(self):
        from pincer.channels.whatsapp_store import describe_store, libpq_conninfo

        url, sp = libpq_conninfo(_PG_URL)
        assert sp == "whatsapp"
        assert url.endswith("?sslmode=disable&options=-c%20search_path%3Dwhatsapp")
        assert describe_store(_PG_URL) == "postgres (schema=whatsapp)"
        assert "hunter2secret" not in describe_store(_PG_URL)
        assert describe_store("/data/pincer-wa.db") == "sqlite /data/pincer-wa.db"

    def test_redact_store(self):
        from pincer.channels.whatsapp_store import redact_store

        msg = f'parse "{_PG_URL}": invalid port; pq: password hunter2secret rejected'
        out = redact_store(msg, _PG_URL)
        assert "hunter2secret" not in out
        assert "<session db url>" in out


def _fake_client_factory(connect_task_factory=None):
    """A NewAClient stand-in that records its kwargs and returns a connect task."""
    created: list[dict] = []

    def factory(**kwargs):
        client = MagicMock()
        client.event = MagicMock()

        async def connect():
            if connect_task_factory is None:
                return None
            return asyncio.get_running_loop().create_task(connect_task_factory())

        client.connect = connect
        created.append(kwargs)
        return client

    return factory, created


@pytest.fixture
def wa_mod(monkeypatch):
    """Patch a name in the globals the channel class actually runs with.

    Other test files del+reimport `pincer.channels.whatsapp`, so the module in
    sys.modules isn't necessarily the one WhatsAppChannel came from.
    """
    wa_globals = WhatsAppChannel.start.__globals__

    def patch(name, value):
        monkeypatch.setitem(wa_globals, name, value)

    patch("neonize_log", MagicMock())
    patch("_neonize_events", None)
    for name in (
        "ConnectedEv",
        "PairStatusEv",
        "MessageEv",
        "ConnectFailureEv",
        "ClientOutdatedEv",
        "LoggedOutEv",
        "StreamErrorEv",
    ):
        patch(name, type(name, (), {}))
    patch("_WA_CONNECT_TIMEOUT", 0.5)
    return patch


@pytest.fixture
def wa_store(monkeypatch):
    """Patch a name in the `whatsapp_store` module the channel class actually calls."""
    store_mod = WhatsAppChannel.start.__globals__["whatsapp_store"]

    def patch(name, value):
        monkeypatch.setattr(store_mod, name, value)

    patch.module = store_mod  # type: ignore[attr-defined]
    return patch


class TestStartSessionStore:
    @pytest.mark.asyncio
    async def test_sqlite_default_keeps_neonize_session_key(self, wa_channel, wa_mod, monkeypatch, tmp_path):
        # No uuid: neonize keys the session by the path, as before, so an
        # already-paired SQLite session resumes unchanged.
        factory, created = _fake_client_factory()
        wa_mod("NewAClient", factory)
        wa_channel._settings.data_dir = tmp_path
        wa_channel._connected.set()

        await wa_channel.start(AsyncMock())

        assert created == [{"name": str(tmp_path / "pincer-wa.db")}]

    @pytest.mark.asyncio
    async def test_postgres_url_is_name_and_never_logged(self, wa_channel, wa_mod, monkeypatch, caplog):
        factory, created = _fake_client_factory()
        wa_mod("NewAClient", factory)
        wa_channel._settings.whatsapp_session_db = SecretStr(_PG_URL)
        wa_channel._connected.set()

        with caplog.at_level(logging.DEBUG):
            await wa_channel.start(AsyncMock())

        assert created[0]["name"] == _PG_URL
        assert created[0]["uuid"] == "pincer-wa"
        assert "session store: postgres (schema=whatsapp)" in caplog.text
        assert "hunter2secret" not in caplog.text
        assert _PG_URL not in caplog.text

    @pytest.mark.asyncio
    async def test_jid_found_passes_jid(self, wa_channel, wa_mod, wa_store, monkeypatch, tmp_path):
        factory, created = _fake_client_factory()
        wa_mod("NewAClient", factory)
        wa_store("build_jid", lambda user, server: MagicMock(User=user, Server=server))
        _make_device_db(tmp_path / "pincer-wa.db", ["491111111111:3@s.whatsapp.net", "492222222222:12@s.whatsapp.net"])
        wa_channel._settings.data_dir = tmp_path
        wa_channel._settings.whatsapp_jid = "492222222222"
        wa_channel._connected.set()

        await wa_channel.start(AsyncMock())

        jid = created[0]["jid"]
        assert (jid.User, jid.Device, jid.Server) == ("492222222222", 12, "s.whatsapp.net")
        assert "new_device" not in created[0]

    @pytest.mark.asyncio
    async def test_jid_missing_pairs_new_device(self, wa_channel, wa_mod, monkeypatch, tmp_path):
        factory, created = _fake_client_factory()
        wa_mod("NewAClient", factory)
        _make_device_db(tmp_path / "pincer-wa.db", ["491111111111:3@s.whatsapp.net"])
        wa_channel._settings.data_dir = tmp_path
        wa_channel._settings.whatsapp_jid = "492222222222"
        wa_channel._connected.set()

        await wa_channel.start(AsyncMock())

        assert created[0]["new_device"] is True
        assert "jid" not in created[0]

    @pytest.mark.asyncio
    async def test_connect_task_error_fails_fast(self, wa_channel, wa_mod, monkeypatch, tmp_path):
        async def boom():
            raise RuntimeError(f'failed to open {_PG_URL}: pq: schema "whatsapp" does not exist')

        factory, _ = _fake_client_factory(boom)
        wa_mod("NewAClient", factory)
        wa_mod("_WA_CONNECT_TIMEOUT", 30)
        wa_channel._settings.whatsapp_session_db = SecretStr(_PG_URL)

        loop = asyncio.get_running_loop()
        t0 = loop.time()
        with pytest.raises(RuntimeError, match="WhatsApp store/connect failed") as exc:
            await wa_channel.start(AsyncMock())
        assert loop.time() - t0 < 5
        assert 'schema "whatsapp" does not exist' in str(exc.value)
        assert "hunter2secret" not in str(exc.value)

    @pytest.mark.asyncio
    async def test_timeout_still_asks_for_qr(self, wa_channel, wa_mod, monkeypatch, tmp_path):
        async def forever():
            await asyncio.sleep(3600)

        factory, _ = _fake_client_factory(forever)
        wa_mod("NewAClient", factory)
        wa_channel._settings.data_dir = tmp_path

        with pytest.raises(RuntimeError, match="Did you scan the QR code"):
            await wa_channel.start(AsyncMock())

    @pytest.mark.asyncio
    async def test_login_error_still_surfaces(self, wa_channel, wa_mod, monkeypatch, tmp_path):
        async def forever():
            await asyncio.sleep(3600)

        factory, _ = _fake_client_factory(forever)
        wa_mod("NewAClient", factory)
        wa_channel._settings.data_dir = tmp_path
        wa_channel._record_login_failure("logged_out", "re-pair")

        with pytest.raises(RuntimeError, match="WhatsApp login failed: logged_out"):
            await wa_channel.start(AsyncMock())


class TestWrongPhoneCheck:
    def _client(self, user):
        me = MagicMock()
        me.JID.User = user
        me.HasField.return_value = False
        client = MagicMock()
        client.get_me = AsyncMock(return_value=me)
        return client

    @pytest.mark.asyncio
    async def test_wrong_phone_logs_error(self, wa_channel, caplog):
        wa_channel._settings.whatsapp_jid = "492222222222"
        with caplog.at_level(logging.ERROR, logger="pincer.channels.whatsapp"):
            await wa_channel._on_connected(self._client("491111111111"), MagicMock())
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert errors and "491111111111" in errors[0].getMessage()
        assert wa_channel._connected.is_set()

    @pytest.mark.asyncio
    async def test_matching_phone_is_quiet(self, wa_channel, caplog):
        wa_channel._settings.whatsapp_jid = "492222222222"
        with caplog.at_level(logging.ERROR, logger="pincer.channels.whatsapp"):
            await wa_channel._on_connected(self._client("492222222222"), MagicMock())
        assert not [r for r in caplog.records if r.levelno == logging.ERROR]


class TestSessionStoreHelpers:
    def test_libpq_conninfo_drops_lib_pq_only_params(self):
        from pincer.channels.whatsapp_store import libpq_conninfo

        url, sp = libpq_conninfo(
            "postgres://u:p@h/db?binary_parameters=yes&sslmode=require&disable_prepared_binary_result=yes"
        )
        assert sp is None
        assert url == "postgres://u:p@h/db?sslmode=require"

    def test_sqlite_other_operational_error_is_raised(self, tmp_path):
        import sqlite3

        from pincer.channels.whatsapp_store import stored_device_jids

        db = tmp_path / "pincer-wa.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE whatsmeow_device (other TEXT)")
        conn.close()
        with pytest.raises(sqlite3.OperationalError, match="no such column"):
            stored_device_jids(str(db))

    @pytest.mark.parametrize(
        ("jid", "expected"),
        [
            ("492222222222.1:12@s.whatsapp.net", ("492222222222", "s.whatsapp.net", 12, 1)),
            ("492222222222@s.whatsapp.net", ("492222222222", "s.whatsapp.net", 0, 0)),
            ("492222222222:3", ("492222222222", "s.whatsapp.net", 3, 0)),
        ],
    )
    def test_parse_device_jid(self, wa_store, jid, expected):
        wa_store("build_jid", lambda user, server: MagicMock(User=user, Server=server))
        parsed = wa_store.module.parse_device_jid(jid)
        assert (parsed.User, parsed.Server, parsed.Device, parsed.RawAgent) == expected

    def test_describe_sqlite(self):
        from pincer.channels.whatsapp_store import describe_store

        assert describe_store("/data/pincer-wa.db") == "sqlite /data/pincer-wa.db"

    def test_redact_sqlite_passthrough(self):
        from pincer.channels.whatsapp_store import redact_store

        assert redact_store("open /data/pincer-wa.db: denied", "/data/pincer-wa.db") == (
            "open /data/pincer-wa.db: denied"
        )

    def test_redact_percent_encoded_password_both_forms(self):
        from pincer.channels.whatsapp_store import redact_store

        store = "postgres://u:p%40ss%2Fword@h/db"
        msg = "pq: password p@ss/word rejected; raw p%40ss%2Fword"
        out = redact_store(msg, store)
        assert "p@ss/word" not in out
        assert "p%40ss%2Fword" not in out
        assert out.count("***") == 2


def _fake_psycopg(monkeypatch, *, rows=(), raise_undefined=False):
    """Install a fake `psycopg` module; return the list of connect() calls."""
    calls: list[tuple[str, dict]] = []

    class UndefinedTable(Exception):
        pass

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql):
            if raise_undefined:
                raise UndefinedTable('relation "whatsmeow_device" does not exist')
            return iter([(r,) for r in rows])

    def connect(conninfo, **kwargs):
        calls.append((conninfo, kwargs))
        return _Conn()

    mod = ModuleType("psycopg")
    mod.connect = connect  # type: ignore[attr-defined]
    mod.errors = ModuleType("psycopg.errors")  # type: ignore[attr-defined]
    mod.errors.UndefinedTable = UndefinedTable  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", mod)
    return calls


class TestStoredDeviceJidsPostgres:
    def test_rows_and_conninfo(self, monkeypatch):
        from pincer.channels.whatsapp_store import stored_device_jids

        calls = _fake_psycopg(monkeypatch, rows=["491111111111:3@s.whatsapp.net"])
        url = "postgres://u:p@h/db?sslmode=disable&binary_parameters=yes&search_path=wa schema\\x"
        assert stored_device_jids(url) == ["491111111111:3@s.whatsapp.net"]
        conninfo, kwargs = calls[0]
        assert parse_qsl(urlsplit(conninfo).query) == [
            ("sslmode", "disable"),
            ("options", "-c search_path=wa\\ schema\\\\x"),
        ]
        assert kwargs == {"connect_timeout": 10}

    def test_no_search_path_no_options(self, monkeypatch):
        from pincer.channels.whatsapp_store import stored_device_jids

        calls = _fake_psycopg(monkeypatch)
        assert stored_device_jids("postgresql://u:p@h/db") == []
        assert calls[0] == ("postgresql://u:p@h/db", {"connect_timeout": 10})

    def test_undefined_table_means_no_devices(self, monkeypatch):
        from pincer.channels.whatsapp_store import stored_device_jids

        _fake_psycopg(monkeypatch, raise_undefined=True)
        assert stored_device_jids(_PG_URL) == []

    def test_psycopg_missing_names_the_extra(self, monkeypatch):
        from pincer.channels.whatsapp_store import stored_device_jids

        monkeypatch.setitem(sys.modules, "psycopg", None)
        with pytest.raises(RuntimeError, match=r"pincer-agent\[postgres\]"):
            stored_device_jids(_PG_URL)


class TestStartFailures:
    @pytest.mark.asyncio
    async def test_store_lookup_error_is_redacted(self, wa_channel, wa_mod, wa_store):
        def boom(store, phone):
            raise RuntimeError(f"cannot reach {store}")

        factory, created = _fake_client_factory()
        wa_mod("NewAClient", factory)
        wa_store("stored_device_jid", boom)
        wa_channel._settings.whatsapp_session_db = SecretStr(_PG_URL)
        wa_channel._settings.whatsapp_jid = "492222222222"

        with pytest.raises(RuntimeError, match="WhatsApp store/connect failed: cannot reach <session db url>") as exc:
            await wa_channel.start(AsyncMock())
        assert "hunter2secret" not in str(exc.value)
        assert created == []

    @pytest.mark.asyncio
    async def test_cancelled_connect_task_means_client_stopped(self, wa_channel, wa_mod, tmp_path):
        async def cancelled():
            raise asyncio.CancelledError

        factory, _ = _fake_client_factory(cancelled)
        wa_mod("NewAClient", factory)
        wa_mod("_WA_LOGIN_ERROR_GRACE", 0.05)
        wa_channel._settings.data_dir = tmp_path

        with pytest.raises(RuntimeError, match="client stopped before connecting"):
            await wa_channel.start(AsyncMock())

    @pytest.mark.asyncio
    async def test_clean_exit_then_login_error_surfaces(self, wa_channel, wa_mod, tmp_path):
        async def logged_out():
            # The LoggedOut handler lands just after the connect task ends.
            asyncio.get_running_loop().call_later(0.05, wa_channel._record_login_failure, "logged_out", "re-pair")

        factory, _ = _fake_client_factory(logged_out)
        wa_mod("NewAClient", factory)
        wa_mod("_WA_LOGIN_ERROR_GRACE", 5)
        wa_channel._settings.data_dir = tmp_path

        with pytest.raises(RuntimeError, match="WhatsApp login failed: logged_out: re-pair"):
            await wa_channel.start(AsyncMock())

    @pytest.mark.asyncio
    async def test_missing_schema_hint(self, wa_channel, wa_mod):
        async def boom():
            raise RuntimeError('pq: relation "whatsmeow_version" does not exist')

        factory, _ = _fake_client_factory(boom)
        wa_mod("NewAClient", factory)
        wa_channel._settings.whatsapp_session_db = SecretStr(_PG_URL)

        with pytest.raises(RuntimeError, match=r"does schema 'whatsapp' exist"):
            await wa_channel.start(AsyncMock())

    @pytest.mark.asyncio
    async def test_timeout_logs_later_failure_redacted(self, wa_channel, wa_mod, caplog):
        release = asyncio.Event()
        tasks: list[asyncio.Task] = []

        async def late_failure():
            tasks.append(asyncio.current_task())
            await release.wait()
            raise RuntimeError(f"failed to open {_PG_URL}")

        factory, _ = _fake_client_factory(late_failure)
        wa_mod("NewAClient", factory)
        wa_mod("_WA_CONNECT_TIMEOUT", 0.05)
        wa_channel._settings.whatsapp_session_db = SecretStr(_PG_URL)

        with pytest.raises(RuntimeError, match="Did you scan the QR code"):
            await wa_channel.start(AsyncMock())

        with caplog.at_level(logging.ERROR, logger="pincer.channels.whatsapp"):
            release.set()
            await asyncio.wait(tasks)
            await asyncio.sleep(0)  # let the done callback run
        assert "WhatsApp client stopped: failed to open <session db url>" in caplog.text
        assert "hunter2secret" not in caplog.text

    @pytest.mark.asyncio
    async def test_log_connect_task_end_quiet_on_cancel_and_success(self, wa_channel, caplog):
        loop = asyncio.get_running_loop()
        cancelled = loop.create_future()
        cancelled.cancel()
        ok = loop.create_future()
        ok.set_result(None)
        with caplog.at_level(logging.DEBUG, logger="pincer.channels.whatsapp"):
            WhatsAppChannel._log_connect_task_end(cancelled, _PG_URL)
            WhatsAppChannel._log_connect_task_end(ok, _PG_URL)
        assert not caplog.records

    @pytest.mark.asyncio
    async def test_wrong_phone_fails_start(self, wa_channel, wa_mod, wa_store, tmp_path):
        me = MagicMock()
        me.JID.User = "491111111111"
        me.HasField.return_value = False
        wa_client = MagicMock()
        wa_client.get_me = AsyncMock(return_value=me)

        async def connects_as_other_phone():
            await wa_channel._on_connected(wa_client, MagicMock())

        factory, _ = _fake_client_factory(connects_as_other_phone)
        wa_mod("NewAClient", factory)
        wa_store("stored_device_jid", lambda store, phone: None)
        wa_channel._settings.data_dir = tmp_path
        wa_channel._settings.whatsapp_jid = "492222222222"

        with pytest.raises(RuntimeError, match="WhatsApp login failed: paired account 491111111111") as exc:
            await wa_channel.start(AsyncMock())
        assert "re-pair with the right phone" in str(exc.value)


class TestReviewHardening:
    """Malformed URLs, redaction gaps, lib/pq params and duplicate devices."""

    @pytest.mark.parametrize("store", ["postgres://u:sekret99@[db:5432/x", "postgres://u:sekret99@h:99999/db"])
    def test_malformed_url_helpers_do_not_raise(self, store):
        from pincer.channels.whatsapp_store import describe_store, redact_store

        assert describe_store(store) in ("postgres (schema=unknown)", "postgres (schema=default)")
        out = redact_store(f"open {store}: pq password=sekret99 rejected; parse postgres://u:sekret99@h/x", store)
        assert "sekret99" not in out

    def test_bracket_url_redacted_wholesale(self):
        from pincer.channels.whatsapp_store import describe_store, redact_store

        store = "postgres://u:pw@[db:5432/x"
        assert describe_store(store) == "postgres (schema=unknown)"
        assert redact_store(f"bad url {store}", store) == "bad url <session db url>"

    def test_query_password_redacted(self):
        from pincer.channels.whatsapp_store import redact_store

        store = "postgres://u@h/db?password=sekret&x=1"
        out = redact_store("pq: auth failed with password=sekret for host=h; dsn host=h password=sekret", store)
        assert "sekret" not in out
        assert out.count("password=***") == 2

    def test_rebuilt_conninfo_redacted(self):
        from pincer.channels.whatsapp_store import libpq_conninfo, redact_store

        store = "postgres://u:hunter2secret@h/db?search_path=wa"
        rebuilt = libpq_conninfo(store)[0]
        assert rebuilt != store
        assert redact_store(f"connect {rebuilt} failed", store) == "connect <session db url> failed"

    def test_short_password_does_not_garble_port(self):
        from pincer.channels.whatsapp_store import redact_store

        store = "postgres://u:54@db:5432/pincer"
        out = redact_store("dial tcp db:5432: refused; url postgres://u:54@db:5432/other", store)
        assert "db:5432: refused" in out
        assert "postgres://u:***@db:5432/other" in out

    def test_unknown_param_moves_into_options(self):
        from pincer.channels.whatsapp_store import libpq_conninfo

        url, _ = libpq_conninfo("postgres://u:p@h/db?statement_timeout=5&sslmode=disable")
        assert parse_qsl(urlsplit(url).query) == [("sslmode", "disable"), ("options", "-c statement_timeout=5")]

    def test_unknown_params_merge_with_existing_options(self):
        from pincer.channels.whatsapp_store import libpq_conninfo

        url, sp = libpq_conninfo(
            "postgres://u:p@h/db?options=-c%20work_mem%3D4MB&search_path=wa&statement_timeout=5&binary_parameters=yes"
        )
        assert sp == "wa"
        assert parse_qsl(urlsplit(url).query) == [
            ("options", "-c work_mem=4MB -c search_path=wa -c statement_timeout=5")
        ]

    def test_duplicate_devices_pick_highest_and_warn(self, tmp_path, caplog):
        from pincer.channels.whatsapp_store import stored_device_jid

        db = tmp_path / "pincer-wa.db"
        _make_device_db(
            db, ["4915:9@s.whatsapp.net", "4915:3@s.whatsapp.net", "4915:12@s.whatsapp.net", "4916:20@s.whatsapp.net"]
        )
        with caplog.at_level(logging.WARNING, logger="pincer.channels.whatsapp_store"):
            assert stored_device_jid(str(db), "4915") == "4915:12@s.whatsapp.net"
        assert "3 stored devices for 4915" in caplog.text
        assert str(db) not in caplog.text

    def test_single_device_no_warning(self, tmp_path, caplog):
        from pincer.channels.whatsapp_store import stored_device_jid

        db = tmp_path / "pincer-wa.db"
        _make_device_db(db, ["4915:3@s.whatsapp.net"])
        with caplog.at_level(logging.WARNING, logger="pincer.channels.whatsapp_store"):
            assert stored_device_jid(str(db), "4915") == "4915:3@s.whatsapp.net"
        assert not caplog.records
