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
            ChannelType.TELEGRAM,
            99999,
            display_name="Test User",
        )
        async with resolver._get_db() as db:
            cursor = await db.execute(
                "SELECT display_name FROM identity_meta WHERE pincer_user_id = ?",
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

    async def test_whatsapp_plus_stripped(self, resolver):
        """Leading + is stripped so +491234 and 491234 resolve to same identity."""
        uid_with = await resolver.resolve(ChannelType.WHATSAPP, "+491234567890")
        uid_without = await resolver.resolve(ChannelType.WHATSAPP, "491234567890")
        assert uid_with == uid_without

    async def test_link_if_new_back_links(self, tmp_path):
        """link_if_new should add a second channel_user_id for the same user."""
        db_path = tmp_path / "link.db"
        r = IdentityResolver(db_path)
        await r.ensure_table()

        # User identified by phone first
        uid = await r.resolve(ChannelType.WHATSAPP, "491234567890")

        # Their LID is not yet in the DB
        async with r._get_db() as db:
            cursor = await db.execute(
                "SELECT pincer_user_id FROM channel_identities "
                "WHERE channel = 'whatsapp' AND channel_user_id = '35240793874528'",
            )
            assert await cursor.fetchone() is None

        # Back-link the LID
        await r.link_if_new(uid, ChannelType.WHATSAPP, "35240793874528")

        # Now both IDs resolve to the same user
        lid_uid = await r.resolve(ChannelType.WHATSAPP, "35240793874528")
        assert lid_uid == uid

    async def test_link_if_new_idempotent(self, resolver):
        """link_if_new on an already-linked ID should be a no-op."""
        uid = await resolver.resolve(ChannelType.TELEGRAM, 12345)
        await resolver.link_if_new(uid, ChannelType.TELEGRAM, 12345)
        uid2 = await resolver.resolve(ChannelType.TELEGRAM, 12345)
        assert uid2 == uid


@pytest.mark.asyncio
class TestActiveChannel:
    async def test_touch_sets_active_channel(self, resolver):
        uid = await resolver.resolve(ChannelType.TELEGRAM, 11111)
        await resolver.link_if_new(uid, ChannelType.WHATSAPP, "491234567890")

        await resolver.touch_active_channel(uid, ChannelType.WHATSAPP)

        async with resolver._get_db() as db:
            cursor = await db.execute(
                "SELECT active_channel, active_channel_updated_at FROM identity_meta WHERE pincer_user_id = ?",
                (uid,),
            )
            row = await cursor.fetchone()
        assert row[0] == "whatsapp"
        assert row[1] is not None

    async def test_get_preferred_channel_prefers_active_over_preferred(self, resolver):
        """preferred_channel is telegram (set at creation); active_channel moves
        to whatsapp after a message comes in from there — get_preferred_channel
        must follow the active one, not the stale creation-time value."""
        uid = await resolver.resolve(ChannelType.TELEGRAM, 22222)
        await resolver.link_if_new(uid, ChannelType.WHATSAPP, "491234567891")

        ch_type, _ = await resolver.get_preferred_channel(uid)
        assert ch_type == ChannelType.TELEGRAM  # unchanged: no active_channel set yet

        await resolver.touch_active_channel(uid, ChannelType.WHATSAPP)

        ch_type, chat_id = await resolver.get_preferred_channel(uid)
        assert ch_type == ChannelType.WHATSAPP
        assert chat_id == "491234567891"

    async def test_get_preferred_channel_falls_back_when_active_channel_unlinked(self, resolver):
        """If active_channel points at a channel that's no longer linked
        (e.g. cleaned up), fall back to preferred_channel instead of erroring."""
        uid = await resolver.resolve(ChannelType.TELEGRAM, 33333)

        async with resolver._get_db() as db:
            await db.execute(
                "UPDATE identity_meta SET active_channel = 'whatsapp' WHERE pincer_user_id = ?",
                (uid,),
            )
            await db.commit()

        ch_type, chat_id = await resolver.get_preferred_channel(uid)
        assert ch_type == ChannelType.TELEGRAM
        assert chat_id == "33333"

    async def test_touch_active_channel_unknown_user_is_noop(self, resolver):
        """Touching an identity that doesn't exist shouldn't raise."""
        await resolver.touch_active_channel("usr_nonexistent", ChannelType.TELEGRAM)

    async def test_touch_active_channel_skips_write_when_unchanged(self, resolver):
        """Repeated touches with the same channel don't bump the timestamp needlessly."""
        uid = await resolver.resolve(ChannelType.TELEGRAM, 44444)
        await resolver.touch_active_channel(uid, ChannelType.TELEGRAM)

        async with resolver._get_db() as db:
            cursor = await db.execute(
                "SELECT active_channel_updated_at FROM identity_meta WHERE pincer_user_id = ?", (uid,)
            )
            first_ts = (await cursor.fetchone())[0]

        await resolver.touch_active_channel(uid, ChannelType.TELEGRAM)

        async with resolver._get_db() as db:
            cursor = await db.execute(
                "SELECT active_channel_updated_at FROM identity_meta WHERE pincer_user_id = ?", (uid,)
            )
            second_ts = (await cursor.fetchone())[0]

        assert first_ts == second_ts

    async def test_touch_active_channel_updates_when_channel_changes(self, resolver):
        uid = await resolver.resolve(ChannelType.TELEGRAM, 55556)
        await resolver.link_if_new(uid, ChannelType.WHATSAPP, "491234567892")

        await resolver.touch_active_channel(uid, ChannelType.TELEGRAM)
        await resolver.touch_active_channel(uid, ChannelType.WHATSAPP)

        async with resolver._get_db() as db:
            cursor = await db.execute(
                "SELECT active_channel FROM identity_meta WHERE pincer_user_id = ?", (uid,)
            )
            row = await cursor.fetchone()
        assert row[0] == "whatsapp"

    async def test_max_active_age_ignores_stale_active_channel(self, resolver):
        """active_channel older than max_active_age_seconds falls back to preferred_channel."""
        uid = await resolver.resolve(ChannelType.TELEGRAM, 55557)  # preferred_channel = telegram
        await resolver.link_if_new(uid, ChannelType.WHATSAPP, "491234567893")
        await resolver.touch_active_channel(uid, ChannelType.WHATSAPP)

        # Backdate active_channel_updated_at by 31 minutes (past the 30-minute window).
        async with resolver._get_db() as db:
            await db.execute(
                "UPDATE identity_meta SET active_channel_updated_at = "
                "datetime('now', '-31 minutes') WHERE pincer_user_id = ?",
                (uid,),
            )
            await db.commit()

        ch_type, _ = await resolver.get_preferred_channel(uid, max_active_age_seconds=1800)
        assert ch_type == ChannelType.TELEGRAM  # fell back, ignoring the stale whatsapp active_channel

    async def test_max_active_age_accepts_fresh_active_channel(self, resolver):
        uid = await resolver.resolve(ChannelType.TELEGRAM, 55558)
        await resolver.link_if_new(uid, ChannelType.WHATSAPP, "491234567894")
        await resolver.touch_active_channel(uid, ChannelType.WHATSAPP)

        ch_type, _ = await resolver.get_preferred_channel(uid, max_active_age_seconds=1800)
        assert ch_type == ChannelType.WHATSAPP  # fresh — well within the window

    async def test_max_active_age_none_ignores_staleness(self, resolver):
        """Default (no max age) preserves today's behavior — always trust active_channel."""
        uid = await resolver.resolve(ChannelType.TELEGRAM, 55559)
        await resolver.link_if_new(uid, ChannelType.WHATSAPP, "491234567895")
        await resolver.touch_active_channel(uid, ChannelType.WHATSAPP)

        async with resolver._get_db() as db:
            await db.execute(
                "UPDATE identity_meta SET active_channel_updated_at = "
                "datetime('now', '-1 day') WHERE pincer_user_id = ?",
                (uid,),
            )
            await db.commit()

        ch_type, _ = await resolver.get_preferred_channel(uid)  # no max_active_age_seconds
        assert ch_type == ChannelType.WHATSAPP

    async def test_max_active_age_treats_missing_timestamp_as_stale(self, resolver):
        """active_channel set but active_channel_updated_at NULL (shouldn't normally
        happen, but defensively) is treated as stale, not fresh."""
        uid = await resolver.resolve(ChannelType.TELEGRAM, 55560)
        await resolver.link_if_new(uid, ChannelType.WHATSAPP, "491234567896")

        async with resolver._get_db() as db:
            await db.execute(
                "UPDATE identity_meta SET active_channel = 'whatsapp', active_channel_updated_at = NULL "
                "WHERE pincer_user_id = ?",
                (uid,),
            )
            await db.commit()

        ch_type, _ = await resolver.get_preferred_channel(uid, max_active_age_seconds=1800)
        assert ch_type == ChannelType.TELEGRAM

    async def test_ensure_table_migration_adds_columns_to_existing_db(self, tmp_path):
        """A DB created before this feature (no active_channel columns) must
        migrate cleanly the next time ensure_table() runs."""
        db_path = tmp_path / "legacy_schema.db"

        import aiosqlite

        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute("""
                CREATE TABLE identity_meta (
                    pincer_user_id TEXT PRIMARY KEY,
                    preferred_channel TEXT,
                    display_name TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                )
            """)
            await db.execute(
                "INSERT INTO identity_meta (pincer_user_id, preferred_channel) VALUES ('usr_old', 'telegram')"
            )
            await db.commit()

        r = IdentityResolver(db_path)
        await r.ensure_table()  # must not raise despite the columns already being absent

        # Existing row (and touch_active_channel on it) must keep working post-migration.
        await r.touch_active_channel("usr_old", ChannelType.WHATSAPP)

        async with r._get_db() as db:
            cursor = await db.execute("PRAGMA table_info(identity_meta)")
            col_names = {row[1] for row in await cursor.fetchall()}
        assert "active_channel" in col_names
        assert "active_channel_updated_at" in col_names


@pytest.mark.asyncio
class TestSeedFromConfig:
    async def test_creates_identity(self, tmp_path):
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

    async def test_idempotent(self, tmp_path):
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

    async def test_updates_existing(self, tmp_path):
        """seed_from_config should add the missing channel to an existing identity."""
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

    async def test_conflict_merged_into_name(self, tmp_path):
        """Both channels had separate identities — merge both into the configured name."""
        db_path = tmp_path / "conflict_merge.db"

        r_init = IdentityResolver(db_path, identity_map_config="")
        await r_init.ensure_table()
        tg_uid = await r_init.resolve(ChannelType.TELEGRAM, 12345)
        wa_uid = await r_init.resolve(ChannelType.WHATSAPP, "491234567890")
        assert tg_uid != wa_uid  # separate identities before config

        r = IdentityResolver(
            db_path,
            identity_map_config="john@telegram:12345=whatsapp:491234567890",
        )
        await r.seed_from_config()

        assert await r.resolve(ChannelType.TELEGRAM, 12345) == "john"
        assert await r.resolve(ChannelType.WHATSAPP, "491234567890") == "john"

    async def test_conflict_merged_unnamed(self, tmp_path):
        """When both channels have separate identities and no name, merge right into left."""
        db_path = tmp_path / "conflict_unnamed.db"

        r_init = IdentityResolver(db_path, identity_map_config="")
        await r_init.ensure_table()
        tg_uid = await r_init.resolve(ChannelType.TELEGRAM, 12345)
        wa_uid = await r_init.resolve(ChannelType.WHATSAPP, "491234567890")
        assert tg_uid != wa_uid

        r = IdentityResolver(
            db_path,
            identity_map_config="telegram:12345=whatsapp:491234567890",
        )
        await r.seed_from_config()

        tg_uid_new = await r.resolve(ChannelType.TELEGRAM, 12345)
        wa_uid_new = await r.resolve(ChannelType.WHATSAPP, "491234567890")
        assert tg_uid_new == wa_uid_new
        assert tg_uid_new == tg_uid  # telegram (left) side preserved as canonical

    async def test_empty_config(self, resolver):
        """seed_from_config with empty config should be a no-op."""
        await resolver.seed_from_config()

    async def test_multiple_mappings(self, tmp_path):
        db_path = tmp_path / "seed_multi.db"
        r = IdentityResolver(
            db_path,
            identity_map_config=("telegram:11111=whatsapp:490000000001,telegram:22222=whatsapp:490000000002"),
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

    async def test_three_channel_entry(self, tmp_path):
        """A single identity entry with three channels links all three."""
        db_path = tmp_path / "three_ch.db"
        r = IdentityResolver(
            db_path,
            identity_map_config="john@telegram:12345=whatsapp:491234567890=signal:491234567890",
        )
        await r.ensure_table()
        await r.seed_from_config()

        tg_uid = await r.resolve(ChannelType.TELEGRAM, 12345)
        wa_uid = await r.resolve(ChannelType.WHATSAPP, "491234567890")
        sig_uid = await r.resolve(ChannelType.SIGNAL, "491234567890")
        assert tg_uid == wa_uid == sig_uid == "john"

        channels = await r.get_all_channels("john")
        assert ChannelType.TELEGRAM in channels
        assert ChannelType.WHATSAPP in channels
        assert ChannelType.SIGNAL in channels

    async def test_right_side_existing_linked_to_left(self, tmp_path):
        """When right-side identity exists but left doesn't, left gets linked to right."""
        db_path = tmp_path / "seed_right.db"
        r = IdentityResolver(
            db_path,
            identity_map_config="telegram:12345=whatsapp:491234567890",
        )
        await r.ensure_table()

        # Create the WA identity first
        wa_uid = await r.resolve(ChannelType.WHATSAPP, "491234567890")

        await r.seed_from_config()

        # Telegram should now be linked to the same identity
        tg_uid = await r.resolve(ChannelType.TELEGRAM, 12345)
        assert tg_uid == wa_uid


@pytest.mark.asyncio
class TestNamedCanonicalId:
    async def test_named_id_used_on_seed(self, tmp_path):
        """seed_from_config with name@ prefix should use the name as canonical ID."""
        db_path = tmp_path / "named.db"
        r = IdentityResolver(db_path, identity_map_config="john@telegram:12345=whatsapp:491234567890")
        await r.ensure_table()
        await r.seed_from_config()

        tg_uid = await r.resolve(ChannelType.TELEGRAM, 12345)
        wa_uid = await r.resolve(ChannelType.WHATSAPP, "491234567890")
        assert tg_uid == "john"
        assert wa_uid == "john"

    async def test_named_id_used_on_first_resolve(self, tmp_path):
        """resolve() should use the named canonical ID even without seed_from_config."""
        db_path = tmp_path / "named_resolve.db"
        r = IdentityResolver(db_path, identity_map_config="alice@telegram:999=whatsapp:491000000001")
        await r.ensure_table()

        # Telegram messages first — no seed, name should still apply
        tg_uid = await r.resolve(ChannelType.TELEGRAM, 999)
        assert tg_uid == "alice"

        # WhatsApp resolves to the same named identity
        wa_uid = await r.resolve(ChannelType.WHATSAPP, "491000000001")
        assert wa_uid == "alice"

    async def test_named_id_whatsapp_first(self, tmp_path):
        """Named ID works when the right-hand channel messages first."""
        db_path = tmp_path / "named_wa_first.db"
        r = IdentityResolver(db_path, identity_map_config="bob@telegram:111=whatsapp:491000000002")
        await r.ensure_table()

        wa_uid = await r.resolve(ChannelType.WHATSAPP, "491000000002")
        assert wa_uid == "bob"

        tg_uid = await r.resolve(ChannelType.TELEGRAM, 111)
        assert tg_uid == "bob"

    async def test_hash_identity_renamed_to_name(self, tmp_path):
        """Adding name@ to an existing hash-based identity renames it on next seed."""
        db_path = tmp_path / "named_existing.db"

        # First: identity created with no config — gets hash-based ID
        r_no_config = IdentityResolver(db_path, identity_map_config="")
        await r_no_config.ensure_table()
        original_uid = await r_no_config.resolve(ChannelType.TELEGRAM, 222)
        assert original_uid.startswith("usr_")

        # Later: admin adds a name to the config and restarts (seed is called on startup)
        r_with_name = IdentityResolver(db_path, identity_map_config="carol@telegram:222=whatsapp:491000000003")
        await r_with_name.seed_from_config()

        # Hash identity should now be renamed to "carol"
        tg_uid = await r_with_name.resolve(ChannelType.TELEGRAM, 222)
        assert tg_uid == "carol"

        # Old hash-based identity should no longer exist
        async with r_with_name._get_db() as db:
            cursor = await db.execute(
                "SELECT pincer_user_id FROM identity_meta WHERE pincer_user_id = ?",
                (original_uid,),
            )
            assert await cursor.fetchone() is None

    async def test_explicit_name_not_overridden_by_different_name(self, tmp_path):
        """An already-named identity is not overridden by a different name in config."""
        db_path = tmp_path / "double_name.db"

        # First boot: identity created as "dave"
        r1 = IdentityResolver(db_path, identity_map_config="dave@telegram:333=whatsapp:491000000007")
        await r1.ensure_table()
        await r1.seed_from_config()
        assert await r1.resolve(ChannelType.TELEGRAM, 333) == "dave"

        # Config accidentally changed to "david"
        r2 = IdentityResolver(db_path, identity_map_config="david@telegram:333=whatsapp:491000000007")
        await r2.seed_from_config()  # warns, keeps "dave"
        assert await r2.resolve(ChannelType.TELEGRAM, 333) == "dave"

    async def test_unnamed_entry_still_works(self, tmp_path):
        """Entries without a name@ prefix keep the auto-generated hash behavior."""
        db_path = tmp_path / "unnamed.db"
        r = IdentityResolver(db_path, identity_map_config="telegram:333=whatsapp:491000000004")
        await r.ensure_table()
        await r.seed_from_config()

        expected = IdentityResolver._generate_user_id(ChannelType.TELEGRAM, "333")
        tg_uid = await r.resolve(ChannelType.TELEGRAM, 333)
        assert tg_uid == expected

    async def test_parse_mapping_named(self):
        name, pairs = IdentityResolver._parse_mapping("john@telegram:12345=whatsapp:491234567890")
        assert name == "john"
        assert pairs[0] == ("telegram", "12345")
        assert pairs[1] == ("whatsapp", "491234567890")

    async def test_parse_mapping_unnamed(self):
        name, pairs = IdentityResolver._parse_mapping("telegram:12345=whatsapp:491234567890")
        assert name is None
        assert pairs[0] == ("telegram", "12345")

    async def test_parse_mapping_three_channels(self):
        name, pairs = IdentityResolver._parse_mapping("john@telegram:johnDoe=whatsapp:491234567890=signal:491234567890")
        assert name == "john"
        assert len(pairs) == 3
        assert pairs[0] == ("telegram", "johnDoe")
        assert pairs[1] == ("whatsapp", "491234567890")
        assert pairs[2] == ("signal", "491234567890")

    async def test_mixed_named_and_unnamed(self, tmp_path):
        """Config can mix named and unnamed entries."""
        db_path = tmp_path / "mixed.db"
        r = IdentityResolver(
            db_path,
            identity_map_config="dave@telegram:444=whatsapp:491000000005,telegram:555=whatsapp:491000000006",
        )
        await r.ensure_table()
        await r.seed_from_config()

        assert await r.resolve(ChannelType.TELEGRAM, 444) == "dave"

        uid555 = await r.resolve(ChannelType.TELEGRAM, 555)
        assert uid555 == IdentityResolver._generate_user_id(ChannelType.TELEGRAM, "555")
        assert uid555 != "dave"


@pytest.mark.asyncio
class TestCleanup:
    async def test_cleanup_no_op_without_config(self, tmp_path):
        """cleanup() with no identity_map_config is a no-op — nothing to enforce."""
        import aiosqlite

        db_path = tmp_path / "no_config.db"
        r = IdentityResolver(db_path, identity_map_config="")
        await r.ensure_table()
        uid = await r.resolve(ChannelType.TELEGRAM, 12345)
        await r.cleanup()

        # Row must still exist
        async with (
            aiosqlite.connect(str(db_path)) as db,
            db.execute("SELECT COUNT(*) FROM channel_identities WHERE channel_user_id = '12345'") as cur,
        ):
            assert (await cur.fetchone())[0] == 1
        assert await r.resolve(ChannelType.TELEGRAM, 12345) == uid

    async def test_cleanup_removes_unlisted_channel_links(self, tmp_path):
        """Channel links not in the config whitelist are deleted on cleanup."""
        import aiosqlite

        db_path = tmp_path / "cleanup_listed.db"
        r = IdentityResolver(db_path, identity_map_config="telegram:12345=whatsapp:491234567890")
        await r.ensure_table()
        await r.seed_from_config()

        # Manually insert an extra unlisted channel link
        uid = await r.resolve(ChannelType.TELEGRAM, 12345)
        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute(
                "INSERT INTO channel_identities (channel, channel_user_id, pincer_user_id) "
                "VALUES ('whatsapp', '35240793874528', ?)",
                (uid,),
            )
            await db.commit()

        await r.cleanup()

        async with aiosqlite.connect(str(db_path)) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM channel_identities WHERE channel_user_id = '35240793874528'"
            ) as cur:
                assert (await cur.fetchone())[0] == 0
            # Listed channels survive
            async with db.execute("SELECT COUNT(*) FROM channel_identities WHERE channel_user_id = '12345'") as cur:
                assert (await cur.fetchone())[0] == 1

    async def test_cleanup_removes_channelless_identity_after_purge(self, tmp_path):
        """identity_meta rows left with no channels after cleanup are also deleted."""
        import aiosqlite

        db_path = tmp_path / "channelless.db"
        # Config only covers telegram:12345; the whatsapp-only identity has no listed channels
        r = IdentityResolver(db_path, identity_map_config="telegram:12345=whatsapp:491234567890")
        await r.ensure_table()

        # Create an identity linked only to an unlisted channel
        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute(
                "INSERT INTO identity_meta (pincer_user_id, preferred_channel) VALUES ('ghost', 'whatsapp')"
            )
            await db.execute(
                "INSERT INTO channel_identities (channel, channel_user_id, pincer_user_id) "
                "VALUES ('whatsapp', '35240793874528', 'ghost')"
            )
            await db.commit()

        await r.cleanup()

        async with (
            aiosqlite.connect(str(db_path)) as db,
            db.execute("SELECT COUNT(*) FROM identity_meta WHERE pincer_user_id = 'ghost'") as cur,
        ):
            assert (await cur.fetchone())[0] == 0

    async def test_cleanup_no_op_when_tables_absent(self, tmp_path):
        """cleanup() on a DB without the new schema tables should not raise."""
        import aiosqlite

        db_path = tmp_path / "empty.db"
        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute("CREATE TABLE unrelated (x INTEGER)")
            await db.commit()

        r = IdentityResolver(db_path, identity_map_config="telegram:12345=whatsapp:491234567890")
        await r.cleanup()  # must not raise


@pytest.mark.asyncio
class TestLegacyMigration:
    async def test_migrates_old_schema(self, tmp_path):
        """Data in the old identity_map table should be accessible after migration."""
        import aiosqlite

        db_path = tmp_path / "legacy.db"

        # Build old schema manually
        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute("""
                CREATE TABLE identity_map (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pincer_user_id TEXT NOT NULL UNIQUE,
                    telegram_user_id INTEGER,
                    whatsapp_phone TEXT,
                    discord_user_id TEXT,
                    display_name TEXT,
                    preferred_channel TEXT DEFAULT 'telegram',
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """)
            await db.execute(
                "INSERT INTO identity_map (pincer_user_id, telegram_user_id, whatsapp_phone, preferred_channel) "
                "VALUES ('usr_legacy01', 12345, '491234567890', 'telegram')"
            )
            await db.commit()

        r = IdentityResolver(db_path)
        await r.ensure_table()  # triggers migration

        # Both IDs should resolve to the migrated pincer_user_id
        tg_uid = await r.resolve(ChannelType.TELEGRAM, 12345)
        wa_uid = await r.resolve(ChannelType.WHATSAPP, "491234567890")
        assert tg_uid == "usr_legacy01"
        assert wa_uid == "usr_legacy01"

        channels = await r.get_all_channels("usr_legacy01")
        assert ChannelType.TELEGRAM in channels
        assert ChannelType.WHATSAPP in channels
