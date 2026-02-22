"""Tests for cross-channel identity resolver."""

import pytest
import pytest_asyncio
import aiosqlite

from pincer.channels.base import ChannelType
from pincer.core.identity import IdentityResolver


@pytest_asyncio.fixture
async def resolver(tmp_path):
    db_path = tmp_path / "pincer.db"
    r = IdentityResolver(db_path, identity_map_config="")
    await r.ensure_table()
    yield r


@pytest.mark.asyncio
class TestIdentityResolver:
    async def test_create_telegram_identity(self, resolver):
        uid = await resolver.resolve(ChannelType.TELEGRAM, 12345)
        assert uid.startswith("usr_")
        assert len(uid) > 4

    async def test_create_whatsapp_identity(self, resolver):
        uid = await resolver.resolve(ChannelType.WHATSAPP, "491234567890")
        assert uid.startswith("usr_")

    async def test_idempotent(self, resolver):
        id1 = await resolver.resolve(ChannelType.TELEGRAM, 12345)
        id2 = await resolver.resolve(ChannelType.TELEGRAM, 12345)
        assert id1 == id2

    async def test_different_channels_different_ids(self, resolver):
        tg_id = await resolver.resolve(ChannelType.TELEGRAM, 12345)
        wa_id = await resolver.resolve(ChannelType.WHATSAPP, "491234567890")
        assert tg_id != wa_id

    async def test_display_name_stored(self, resolver):
        uid = await resolver.resolve(
            ChannelType.TELEGRAM, 99999, display_name="Test User",
        )
        async with resolver._get_db() as db:
            cursor = await db.execute(
                "SELECT display_name FROM identity_map WHERE pincer_user_id = ?",
                (uid,),
            )
            row = await cursor.fetchone()
            assert row[0] == "Test User"

    async def test_config_mapping_links(self, tmp_path):
        """Two channels with config mapping should resolve to the same user."""
        db_path = tmp_path / "link_test.db"
        r = IdentityResolver(
            db_path,
            identity_map_config="telegram:12345=whatsapp:491234567890",
        )
        await r.ensure_table()

        tg_id = await r.resolve(ChannelType.TELEGRAM, 12345)
        wa_id = await r.resolve(ChannelType.WHATSAPP, "491234567890")
        assert tg_id == wa_id

    async def test_get_preferred_channel(self, resolver):
        uid = await resolver.resolve(ChannelType.TELEGRAM, 55555)
        ch_type, chat_id = await resolver.get_preferred_channel(uid)
        assert ch_type == ChannelType.TELEGRAM
        assert chat_id == "55555"

    async def test_get_all_channels(self, resolver):
        uid = await resolver.resolve(ChannelType.TELEGRAM, 77777)
        channels = await resolver.get_all_channels(uid)
        assert ChannelType.TELEGRAM in channels
        assert channels[ChannelType.TELEGRAM] == "77777"

    async def test_unknown_user_raises(self, resolver):
        with pytest.raises(ValueError, match="Unknown user"):
            await resolver.get_preferred_channel("usr_nonexistent")

    async def test_deterministic_user_id(self, resolver):
        """Same channel+user_id always produces the same pincer_user_id."""
        id1 = IdentityResolver._generate_user_id(ChannelType.TELEGRAM, 12345)
        id2 = IdentityResolver._generate_user_id(ChannelType.TELEGRAM, 12345)
        assert id1 == id2
