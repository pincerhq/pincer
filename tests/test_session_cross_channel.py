"""Tests for cross-channel session persistence."""

import pytest
import pytest_asyncio

from pincer.channels.base import ChannelType
from pincer.core.identity import IdentityResolver
from pincer.core.session import SessionManager
from pincer.llm.base import LLMMessage, MessageRole


@pytest_asyncio.fixture
async def identity_resolver(tmp_path):
    db_path = tmp_path / "identity.db"
    r = IdentityResolver(
        db_path,
        identity_map_config="telegram:12345=whatsapp:491234567890",
    )
    await r.ensure_table()
    await r.seed_from_config()
    return r


@pytest_asyncio.fixture
async def session_manager(tmp_path):
    sm = SessionManager(tmp_path / "sessions.db", max_messages=20)
    await sm.initialize()
    yield sm
    await sm.close()


@pytest.mark.asyncio
async def test_session_shared_across_channels(
    identity_resolver, session_manager
):
    """Session created on Telegram should be shared when accessed via WhatsApp."""
    uid_tg = await identity_resolver.resolve(ChannelType.TELEGRAM, 12345)
    uid_wa = await identity_resolver.resolve(ChannelType.WHATSAPP, "491234567890")
    assert uid_tg == uid_wa

    # Create session via "Telegram" path (pincer_user_id set)
    session_tg = await session_manager.get_or_create(
        user_id="12345",
        channel="telegram",
        pincer_user_id=uid_tg,
    )
    session_tg.messages.append(
        LLMMessage(role=MessageRole.USER, content="Hello from Telegram")
    )
    await session_manager.add_message(session_tg, session_tg.messages[-1])

    # Create session via "WhatsApp" path — should get same session
    session_wa = await session_manager.get_or_create(
        user_id="491234567890",
        channel="whatsapp",
        pincer_user_id=uid_wa,
    )
    assert session_tg.session_id == session_wa.session_id
    assert len(session_wa.messages) > 0
    assert "Hello from Telegram" in str(session_wa.messages)
