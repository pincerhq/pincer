"""Tests for WhatsApp channel."""

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

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
        settings.whatsapp_group_trigger = "pincer"
        settings.data_dir = "/tmp/test"
        ch = WhatsAppChannel(settings)
        assert len(ch._dm_allowlist) == 0

    def test_mention_by_trigger_word(self, wa_channel):
        wa_channel._own_jid = "491234567890"
        msg = MagicMock()
        msg.conversation = "hey @pincer what's up?"
        msg.extendedTextMessage = None
        assert wa_channel._is_mentioned_in_group(msg, MagicMock()) is True

    def test_mention_by_jid(self, wa_channel):
        wa_channel._own_jid = "491234567890"
        msg = MagicMock()
        msg.conversation = "hey 491234567890 what's up?"
        msg.extendedTextMessage = None
        assert wa_channel._is_mentioned_in_group(msg, MagicMock()) is True

    def test_no_mention(self, wa_channel):
        wa_channel._own_jid = "491234567890"
        msg = MagicMock()
        msg.conversation = "hey everyone, lunch?"
        msg.extendedTextMessage = None
        assert wa_channel._is_mentioned_in_group(msg, MagicMock()) is False

    def test_trigger_case_insensitive(self, wa_channel):
        wa_channel._own_jid = "491234567890"
        msg = MagicMock()
        msg.conversation = "Hey PINCER, do this"
        msg.extendedTextMessage = None
        assert wa_channel._is_mentioned_in_group(msg, MagicMock()) is True
