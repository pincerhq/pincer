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
        "neonize.utils.enum",
        "neonize.client",
        "magic",
    ):
        if name not in sys.modules:
            sys.modules[name] = ModuleType(name)

    neonize_client = sys.modules["neonize.aioze.client"]
    neonize_client.NewAClient = MagicMock  # type: ignore[attr-defined]

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


_ensure_neonize_mocks()

if "pincer.channels.whatsapp" in sys.modules:
    del sys.modules["pincer.channels.whatsapp"]

from pincer.channels.whatsapp import WhatsAppChannel  # noqa: E402

OWNER_PHONE = "491622549781"
OTHER_PHONE = "4917612345678"


def _make_settings(
    whatsapp_self_chat_only: bool = True,
    whatsapp_group_trigger: str = "pincer",
):
    settings = MagicMock()
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
    """Channel with self-chat-only, no identity configured; handler and _extract_message mocked."""
    settings = _make_settings(whatsapp_self_chat_only=True)
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

    async def test_incoming_dm_self_chat_only_ignored(self, routing_channel):
        """With self_chat_only=True, an incoming DM is always ignored, regardless of identity config."""
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

    async def test_incoming_dm_self_chat_only_ignored_with_identity_configured(self):
        """self_chat_only=True still blocks the DM even when an identity map exists."""
        settings = _make_settings(whatsapp_self_chat_only=True)
        ch = WhatsAppChannel(settings, identity=AsyncMock())
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

    async def test_incoming_dm_guest_rejected_when_identity_configured(self):
        """Sender not in identity map, guests not allowed → rejected."""
        settings = _make_settings(whatsapp_self_chat_only=False)
        settings.whatsapp_guests_allowed = False
        ch = WhatsAppChannel(settings, identity=AsyncMock())
        ch._identity.is_guest = AsyncMock(return_value=True)
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

    async def test_incoming_dm_guest_allowed_when_flag_true(self):
        """Sender not in identity map, guests allowed → responds."""
        settings = _make_settings(whatsapp_self_chat_only=False)
        settings.whatsapp_guests_allowed = True
        ch = WhatsAppChannel(settings, identity=AsyncMock())
        ch._identity.is_guest = AsyncMock(return_value=True)
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
        ch._identity.is_guest.assert_not_called()

    async def test_incoming_dm_no_identity_map_accepts_any_sender(self):
        """No IdentityResolver wired in (identity=None) → guest gate is a no-op, so
        self_chat_only=False with no identity map accepts DMs from anyone. This is an
        intentional behavior change from the old empty-DM-allowlist default (which
        blocked everyone) now that the legacy allowlist has been removed."""
        settings = _make_settings(whatsapp_self_chat_only=False)
        settings.whatsapp_guests_allowed = False
        ch = WhatsAppChannel(settings)
        assert ch._identity is None
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

    async def test_group_mention_guest_rejected_when_identity_configured(self):
        """Group mention from a sender not in the identity map, guests not allowed → rejected."""
        settings = _make_settings(whatsapp_self_chat_only=False)
        settings.whatsapp_guests_allowed = False
        ch = WhatsAppChannel(settings, identity=AsyncMock())
        ch._identity.is_guest = AsyncMock(return_value=True)
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
            chat_user="group123",
            sender_user=OTHER_PHONE,
            is_group=True,
            message_text=f"hey {OWNER_PHONE} what's up?",
        )
        await ch._on_message(MagicMock(), event)
        ch._handler.assert_not_called()
        ch._extract_message.assert_not_called()
        ch._identity.is_guest.assert_awaited_once_with(ChannelType.WHATSAPP, OTHER_PHONE)

    async def test_group_mention_guest_allowed_when_flag_true(self):
        """Group mention from a sender not in the identity map, guests allowed → responds."""
        settings = _make_settings(whatsapp_self_chat_only=False)
        settings.whatsapp_guests_allowed = True
        ch = WhatsAppChannel(settings, identity=AsyncMock())
        ch._identity.is_guest = AsyncMock(return_value=True)
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
            chat_user="group123",
            sender_user=OTHER_PHONE,
            is_group=True,
            message_text=f"hey {OWNER_PHONE} what's up?",
        )
        await ch._on_message(MagicMock(), event)
        ch._handler.assert_called_once()
        ch._identity.is_guest.assert_not_called()

    async def test_group_mention_from_owner_from_me_responds(self, routing_channel):
        """Owner's own number posts a mention in a group (from_me=True) → responds.

        Regression test: this previously hit Rule 3's unconditional
        `if is_from_me: return` before the mention check ever ran, so the
        owner could never summon Pincer in a group when Pincer is linked to
        the owner's own WhatsApp number.
        """
        ch = routing_channel
        event, _, _ = _make_message_event(
            from_me=True,
            chat_user="group123",
            sender_user=OWNER_PHONE,
            is_group=True,
            message_text=f"hey {OWNER_PHONE} what's up?",
        )
        await ch._on_message(MagicMock(), event)
        ch._handler.assert_called_once()
        ch._extract_message.assert_called_once()

    async def test_group_from_me_without_mention_still_ignored(self, routing_channel):
        """Owner's own number posts in a group without a mention/trigger → still ignored."""
        ch = routing_channel
        event, _, _ = _make_message_event(
            from_me=True,
            chat_user="group123",
            sender_user=OWNER_PHONE,
            is_group=True,
            message_text="hey everyone",
        )
        await ch._on_message(MagicMock(), event)
        ch._handler.assert_not_called()
        ch._extract_message.assert_not_called()

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
