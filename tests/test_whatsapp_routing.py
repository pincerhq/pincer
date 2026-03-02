"""Tests for WhatsApp message routing (self-chat vs outgoing vs allowlist vs groups)."""

import sys
import time
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pincer.channels.base import ChannelType, IncomingMessage


def _ensure_neonize_mocks():
    """Pre-populate sys.modules with neonize stubs so whatsapp.py can import."""
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
            sys.modules[name] = ModuleType(name)

    neonize_client = sys.modules["neonize.aioze.client"]
    neonize_client.NewAClient = MagicMock  # type: ignore[attr-defined]

    neonize_events = sys.modules["neonize.aioze.events"]
    for ev in ("ConnectedEv", "MessageEv", "PairStatusEv", "QREv"):
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


_ensure_neonize_mocks()

if "pincer.channels.whatsapp" in sys.modules:
    del sys.modules["pincer.channels.whatsapp"]

from pincer.channels.whatsapp import WhatsAppChannel  # noqa: E402

OWNER_PHONE = "491622549781"
OTHER_PHONE = "4917612345678"


def _make_settings(
    dm_allowlist: str = "",
    whatsapp_self_chat_only: bool = True,
    whatsapp_group_trigger: str = "pincer",
):
    settings = MagicMock()
    settings.whatsapp_dm_allowlist = dm_allowlist
    settings.whatsapp_self_chat_only = whatsapp_self_chat_only
    settings.whatsapp_group_trigger = whatsapp_group_trigger
    settings.data_dir = "/tmp/test"
    settings.openai_api_key.get_secret_value.return_value = "test-key"
    return settings


def _make_message_event(
    *,
    from_me: bool,
    chat_user: str,
    sender_user: str,
    is_group: bool = False,
    chat_jid: str = "1234@s.whatsapp.net",
    chat_server: str = "s.whatsapp.net",
    sender_server: str = "s.whatsapp.net",
    msg_id: str = "test-msg-id",
    mentioned_jid: str | None = None,
    message_text: str = "hello",
):
    """Build a minimal MessageEv-like mock for routing tests."""
    source = MagicMock()
    source.IsFromMe = from_me
    source.IsGroup = is_group
    source.Chat = MagicMock()
    source.Chat.User = chat_user
    source.Chat.Server = chat_server
    source.Sender = MagicMock()
    source.Sender.User = sender_user
    source.Sender.Server = sender_server

    info = MagicMock()
    info.ID = msg_id
    info.MessageSource = source
    info.Type = "text"
    info.Timestamp = MagicMock()
    info.Timestamp.seconds = int(time.time()) - 10

    msg = MagicMock()
    msg.conversation = message_text

    ext = MagicMock()
    ext.text = message_text
    has_context = mentioned_jid is not None
    if has_context:
        ctx = MagicMock()
        ctx.mentionedJID = [mentioned_jid]
        ext.contextInfo = ctx
    else:
        ext.contextInfo = None
    ext.HasField = MagicMock(side_effect=lambda f: f == "contextInfo" and has_context)

    msg.extendedTextMessage = ext
    msg.HasField = MagicMock(side_effect=lambda f: f == "extendedTextMessage")

    event = MagicMock()
    event.Message = msg
    event.Info = info
    return event, source, chat_jid


@pytest.fixture
def routing_channel():
    """Channel with empty allowlist, self-chat-only, handler and _extract_message mocked."""
    settings = _make_settings(dm_allowlist="", whatsapp_self_chat_only=True)
    ch = WhatsAppChannel(settings)
    ch._own_jid = OWNER_PHONE
    ch._handler = AsyncMock(return_value="ok")
    ch._extract_message = AsyncMock(
        return_value=IncomingMessage(
            user_id=OWNER_PHONE,
            channel="whatsapp",
            text="hello",
            channel_type=ChannelType.WHATSAPP,
            reply_to_message_id="test-msg-id",
        )
    )
    return ch


@pytest.mark.asyncio
class TestWhatsAppRouting:
    async def test_self_chat_responds(self, routing_channel):
        """Owner messages themselves → Pincer responds."""
        ch = routing_channel
        event, source, chat_jid = _make_message_event(
            from_me=True,
            chat_user=OWNER_PHONE,
            sender_user=OWNER_PHONE,
            is_group=False,
            chat_jid=f"{OWNER_PHONE}@s.whatsapp.net",
        )
        jid_effect = [f"{OWNER_PHONE}@s.whatsapp.net", f"{OWNER_PHONE}@s.whatsapp.net"]
        with patch("pincer.channels.whatsapp.Jid2String", side_effect=jid_effect):
            await ch._on_message(MagicMock(), event)
        ch._handler.assert_called_once()
        ch._extract_message.assert_called_once()

    async def test_self_chat_lid_responds(self, routing_channel):
        """Owner messages themselves via LID JID → Pincer responds.

        The sender also has @lid server, so the owner LID is learned
        from source.Sender before _is_self_chat runs.
        """
        ch = routing_channel
        lid_user = "207855026221128"
        event, source, chat_jid = _make_message_event(
            from_me=True,
            chat_user=lid_user,
            sender_user=lid_user,
            is_group=False,
            chat_jid=f"{lid_user}@lid",
            chat_server="lid",
            sender_server="lid",
        )
        jid_effect = [f"{lid_user}@lid", f"{lid_user}@lid"]
        with patch("pincer.channels.whatsapp.Jid2String", side_effect=jid_effect):
            await ch._on_message(MagicMock(), event)
        ch._handler.assert_called_once()
        ch._extract_message.assert_called_once()
        assert ch._own_lid == lid_user

    async def test_outgoing_to_other_lid_ignored(self, routing_channel):
        """Owner sends message to another person via LID JID → Pincer ignores.

        Even though the chat uses @lid, the other person's LID does not
        match the owner's LID, so it is not self-chat.
        """
        ch = routing_channel
        owner_lid = "207855026221128"
        other_lid = "998877665544332"
        ch._own_lid = owner_lid
        event, _, _ = _make_message_event(
            from_me=True,
            chat_user=other_lid,
            sender_user=owner_lid,
            is_group=False,
            chat_jid=f"{other_lid}@lid",
            chat_server="lid",
            sender_server="lid",
        )
        jid_effect = [f"{other_lid}@lid", f"{owner_lid}@lid"]
        with patch("pincer.channels.whatsapp.Jid2String", side_effect=jid_effect):
            await ch._on_message(MagicMock(), event)
        ch._handler.assert_not_called()
        ch._extract_message.assert_not_called()

    async def test_outgoing_to_other_ignored(self, routing_channel):
        """Owner sends message to someone else → Pincer ignores."""
        ch = routing_channel
        event, _, _ = _make_message_event(
            from_me=True,
            chat_user=OTHER_PHONE,
            sender_user=OWNER_PHONE,
            is_group=False,
            chat_jid=f"{OTHER_PHONE}@s.whatsapp.net",
        )
        jid_effect = [f"{OTHER_PHONE}@s.whatsapp.net", f"{OTHER_PHONE}@s.whatsapp.net"]
        with patch("pincer.channels.whatsapp.Jid2String", side_effect=jid_effect):
            await ch._on_message(MagicMock(), event)
        ch._handler.assert_not_called()
        ch._extract_message.assert_not_called()

    async def test_incoming_dm_no_allowlist_ignored(self, routing_channel):
        """Someone DMs owner, not on allowlist (empty allowlist) → ignored."""
        ch = routing_channel
        event, _, _ = _make_message_event(
            from_me=False,
            chat_user=OWNER_PHONE,
            sender_user=OTHER_PHONE,
            is_group=False,
        )
        await ch._on_message(MagicMock(), event)
        ch._handler.assert_not_called()
        ch._extract_message.assert_not_called()

    async def test_incoming_dm_self_chat_only_ignored_even_if_allowlisted(self):
        """With self_chat_only=True, allowlisted contact DM is still ignored."""
        settings = _make_settings(dm_allowlist=OTHER_PHONE, whatsapp_self_chat_only=True)
        ch = WhatsAppChannel(settings)
        ch._own_jid = OWNER_PHONE
        ch._handler = AsyncMock(return_value="ok")
        ch._extract_message = AsyncMock(
            return_value=IncomingMessage(
                user_id=OTHER_PHONE,
                channel="whatsapp",
                text="hi",
                channel_type=ChannelType.WHATSAPP,
                reply_to_message_id="x",
            )
        )
        event, _, _ = _make_message_event(
            from_me=False,
            chat_user=OWNER_PHONE,
            sender_user=OTHER_PHONE,
            is_group=False,
        )
        await ch._on_message(MagicMock(), event)
        ch._handler.assert_not_called()
        ch._extract_message.assert_not_called()

    async def test_incoming_dm_allowlisted_responds(self):
        """Allowlisted contact DMs owner when self_chat_only=False → Pincer responds."""
        settings = _make_settings(
            dm_allowlist=OTHER_PHONE,
            whatsapp_self_chat_only=False,
        )
        ch = WhatsAppChannel(settings)
        ch._own_jid = OWNER_PHONE
        ch._handler = AsyncMock(return_value="ok")
        ch._extract_message = AsyncMock(
            return_value=IncomingMessage(
                user_id=OTHER_PHONE,
                channel="whatsapp",
                text="hi",
                channel_type=ChannelType.WHATSAPP,
                reply_to_message_id="x",
            )
        )
        event, _, _ = _make_message_event(
            from_me=False,
            chat_user=OWNER_PHONE,
            sender_user=OTHER_PHONE,
            is_group=False,
        )
        await ch._on_message(MagicMock(), event)
        ch._handler.assert_called_once()
        ch._extract_message.assert_called_once()

    async def test_status_broadcast_ignored(self, routing_channel):
        """Status broadcasts always ignored."""
        ch = routing_channel
        event, _, _ = _make_message_event(
            from_me=True,
            chat_user="status",
            sender_user=OWNER_PHONE,
            is_group=False,
            chat_jid="status@broadcast",
        )
        with patch("pincer.channels.whatsapp.Jid2String", return_value="status@broadcast"):
            await ch._on_message(MagicMock(), event)
        ch._handler.assert_not_called()
        ch._extract_message.assert_not_called()

    async def test_group_without_mention_ignored(self, routing_channel):
        """Group message without @mention or trigger → ignored."""
        ch = routing_channel
        event, _, _ = _make_message_event(
            from_me=False,
            chat_user="group123",
            sender_user=OTHER_PHONE,
            is_group=True,
            message_text="hey everyone",
        )
        await ch._on_message(MagicMock(), event)
        ch._handler.assert_not_called()
        ch._extract_message.assert_not_called()

    async def test_group_with_mention_responds(self, routing_channel):
        """Group message with owner JID in text (mention) → responds."""
        ch = routing_channel
        event, _, _ = _make_message_event(
            from_me=False,
            chat_user="group123",
            sender_user=OTHER_PHONE,
            is_group=True,
            message_text=f"hey {OWNER_PHONE} what's up?",
        )
        await ch._on_message(MagicMock(), event)
        ch._handler.assert_called_once()
        ch._extract_message.assert_called_once()

    async def test_echo_of_own_message_ignored(self, routing_channel):
        """Message ID in recent_sent_ids is skipped (echo prevention)."""
        ch = routing_channel
        ch._recent_sent_ids.add("echo-msg-id")
        event, _, _ = _make_message_event(
            from_me=True,
            chat_user=OWNER_PHONE,
            sender_user=OWNER_PHONE,
            is_group=False,
            msg_id="echo-msg-id",
            chat_jid=f"{OWNER_PHONE}@s.whatsapp.net",
        )
        jid_effect = [f"{OWNER_PHONE}@s.whatsapp.net", f"{OWNER_PHONE}@s.whatsapp.net"]
        with patch("pincer.channels.whatsapp.Jid2String", side_effect=jid_effect):
            await ch._on_message(MagicMock(), event)
        ch._handler.assert_not_called()
        ch._extract_message.assert_not_called()
        assert "echo-msg-id" not in ch._recent_sent_ids


class TestIsSelfChat:
    def test_self_chat_true_when_chat_user_is_owner(self):
        settings = _make_settings()
        ch = WhatsAppChannel(settings)
        ch._own_jid = OWNER_PHONE
        assert ch._is_self_chat(OWNER_PHONE) is True

    def test_self_chat_false_when_chat_user_is_other(self):
        settings = _make_settings()
        ch = WhatsAppChannel(settings)
        ch._own_jid = OWNER_PHONE
        assert ch._is_self_chat(OTHER_PHONE) is False

    def test_self_chat_false_when_own_jid_not_set(self):
        settings = _make_settings()
        ch = WhatsAppChannel(settings)
        ch._own_jid = None
        assert ch._is_self_chat(OWNER_PHONE) is False

    def test_self_chat_true_for_stored_lid(self):
        """Once the owner LID is learned, it matches by chat_user."""
        settings = _make_settings()
        ch = WhatsAppChannel(settings)
        ch._own_jid = OWNER_PHONE
        ch._own_lid = "207855026221128"
        assert ch._is_self_chat("207855026221128") is True

    def test_self_chat_false_for_other_lid(self):
        """Another person's LID does not match the owner."""
        settings = _make_settings()
        ch = WhatsAppChannel(settings)
        ch._own_jid = OWNER_PHONE
        ch._own_lid = "207855026221128"
        assert ch._is_self_chat("998877665544332") is False

    def test_self_chat_false_for_unknown_lid(self):
        """When owner LID is not yet known, unknown LID user is not self-chat."""
        settings = _make_settings()
        ch = WhatsAppChannel(settings)
        ch._own_jid = OWNER_PHONE
        assert ch._is_self_chat("207855026221128") is False
