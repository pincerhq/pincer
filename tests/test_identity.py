"""Tests for cross-channel identity resolver."""

import pytest
import pytest_asyncio

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

    async def test_seed_from_config_creates_identity(self, tmp_path):
        """seed_from_config() should pre-create identity rows from config."""
        db_path = tmp_path / "seed_test.db"
        r = IdentityResolver(
            db_path,
            identity_map_config="telegram:12345=whatsapp:491234567890",
        )
        await r.ensure_table()
        await r.seed_from_config()

        expected_uid = IdentityResolver._generate_user_id(ChannelType.TELEGRAM, "12345")
        ch_type, chat_id = await r.get_preferred_channel(expected_uid)
        assert ch_type == ChannelType.TELEGRAM
        assert chat_id == "12345"

        channels = await r.get_all_channels(expected_uid)
        assert ChannelType.TELEGRAM in channels
        assert ChannelType.WHATSAPP in channels
        assert channels[ChannelType.WHATSAPP] == "491234567890"

    async def test_seed_from_config_idempotent(self, tmp_path):
        """Running seed_from_config() twice should not duplicate or error."""
        db_path = tmp_path / "seed_idem.db"
        r = IdentityResolver(
            db_path,
            identity_map_config="telegram:99999=whatsapp:491111111111",
        )
        await r.ensure_table()
        await r.seed_from_config()
        await r.seed_from_config()

        expected_uid = IdentityResolver._generate_user_id(ChannelType.TELEGRAM, "99999")
        channels = await r.get_all_channels(expected_uid)
        assert ChannelType.TELEGRAM in channels
        assert ChannelType.WHATSAPP in channels

    async def test_seed_from_config_updates_existing(self, tmp_path):
        """seed_from_config() should add missing channel to existing identity."""
        db_path = tmp_path / "seed_update.db"
        r = IdentityResolver(
            db_path,
            identity_map_config="telegram:55555=whatsapp:492222222222",
        )
        await r.ensure_table()

        uid = await r.resolve(ChannelType.TELEGRAM, 55555)
        channels_before = await r.get_all_channels(uid)
        assert ChannelType.WHATSAPP not in channels_before

        await r.seed_from_config()
        channels_after = await r.get_all_channels(uid)
        assert ChannelType.WHATSAPP in channels_after
        assert channels_after[ChannelType.WHATSAPP] == "492222222222"

    async def test_seed_from_config_empty(self, resolver):
        """seed_from_config() with empty config should be a no-op."""
        await resolver.seed_from_config()

    async def test_seed_from_config_multiple_mappings(self, tmp_path):
        """seed_from_config() should handle multiple comma-separated mappings."""
        db_path = tmp_path / "seed_multi.db"
        r = IdentityResolver(
            db_path,
            identity_map_config=(
                "telegram:11111=whatsapp:490000000001,"
                "telegram:22222=whatsapp:490000000002"
            ),
        )
        await r.ensure_table()
        await r.seed_from_config()

        uid1 = IdentityResolver._generate_user_id(ChannelType.TELEGRAM, "11111")
        uid2 = IdentityResolver._generate_user_id(ChannelType.TELEGRAM, "22222")
        assert uid1 != uid2

        ch1 = await r.get_all_channels(uid1)
        ch2 = await r.get_all_channels(uid2)
        assert ch1[ChannelType.WHATSAPP] == "490000000001"
        assert ch2[ChannelType.WHATSAPP] == "490000000002"
