"""Sessions and identity on the repository/service layer, on both dialects.

They share a test module because a rename moves rows in both: an identity's
sessions follow it to its new id.
"""

from __future__ import annotations

import pytest

from pincer.db.engine import get_engine
from pincer.models.identity import ChannelIdentity, IdentityProfile
from pincer.models.sessions import ChatSession
from pincer.services.identity import IdentityService, SeedEntry
from pincer.services.sessions import SessionService

_TABLES = [IdentityProfile.__table__, ChannelIdentity.__table__, ChatSession.__table__]


@pytest.fixture
async def url(db_url: str):
    engine = get_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: IdentityProfile.metadata.drop_all(c, tables=_TABLES))
        await conn.run_sync(lambda c: IdentityProfile.metadata.create_all(c, tables=_TABLES))
    yield db_url
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: IdentityProfile.metadata.drop_all(c, tables=_TABLES))


async def _store_session(url: str, session_id: str, *, user_id: str, channel: str = "telegram", at: float = 1.0):
    await SessionService(url).save(
        session_id=session_id,
        user_id=user_id,
        channel=channel,
        messages="[]",
        metadata="{}",
        created_at=at,
        updated_at=at,
    )


# ── sessions ─────────────────────────────────────────────────────────


async def test_a_session_round_trips(url):
    service = SessionService(url)
    await service.save(
        session_id="unified:usr_a",
        user_id="usr_a",
        channel="telegram",
        messages='[{"role": "user"}]',
        metadata='{"onboarding_complete": "true"}',
        created_at=100.0,
        updated_at=200.0,
    )

    row = await service.load("unified:usr_a", user_id="usr_a", channel="telegram")
    assert row["messages"] == '[{"role": "user"}]'
    assert row["metadata"] == '{"onboarding_complete": "true"}'
    assert (row["created_at"], row["updated_at"]) == (100.0, 200.0)


async def test_saving_again_keeps_the_original_created_at(url):
    service = SessionService(url)
    await _store_session(url, "unified:usr_a", user_id="usr_a", at=100.0)
    await service.save(
        session_id="unified:usr_a",
        user_id="usr_a",
        channel="telegram",
        messages='["later"]',
        metadata="{}",
        created_at=999.0,  # a fresh object would claim "now"; the row keeps its own
        updated_at=300.0,
    )

    row = await service.load("unified:usr_a", user_id="usr_a", channel="telegram")
    assert row["created_at"] == 100.0
    assert row["updated_at"] == 300.0
    assert row["messages"] == '["later"]'


async def test_a_session_stored_under_the_old_key_is_still_found(url):
    """Before cross-channel identities, sessions were keyed `channel:user_id`."""
    await _store_session(url, "telegram:12345", user_id="12345", at=10.0)
    await _store_session(url, "telegram:12345-older", user_id="12345", at=5.0)

    row = await SessionService(url).load("unified:usr_a", user_id="12345", channel="telegram")
    assert row["session_id"] == "telegram:12345"  # the most recently updated one


async def test_an_unknown_session_is_none(url):
    assert await SessionService(url).load("unified:nobody", user_id="nobody", channel="telegram") is None


# ── identity ─────────────────────────────────────────────────────────


async def test_creating_and_linking_an_identity(url):
    service = IdentityService(url)
    await service.create_identity("usr_a", channel="telegram", channel_user_id="12345", display_name="Alice")

    assert await service.user_for_channel("telegram", "12345") == "usr_a"
    assert await service.user_for_channel("telegram", "99999") is None

    await service.link_channel("usr_a", "whatsapp", "4930111")
    assert await service.channels_for("usr_a") == [("telegram", "12345"), ("whatsapp", "4930111")]

    profile = await service.profile("usr_a")
    assert (profile["display_name"], profile["preferred_channel"]) == ("Alice", "telegram")


async def test_creating_an_identity_twice_keeps_the_first(url):
    service = IdentityService(url)
    await service.create_identity("usr_a", channel="telegram", channel_user_id="12345", display_name="Alice")
    await service.create_identity("usr_a", channel="telegram", channel_user_id="12345", display_name="Impostor")
    assert (await service.profile("usr_a"))["display_name"] == "Alice"


async def test_renaming_moves_the_profile_links_and_sessions(url):
    service = IdentityService(url)
    await service.create_identity("usr_hash", channel="telegram", channel_user_id="12345", display_name="Alice")
    await _store_session(url, "unified:usr_hash", user_id="usr_hash")

    await service.rename_identity("usr_hash", "alice")

    assert await service.user_for_channel("telegram", "12345") == "alice"
    assert await service.profile("usr_hash") is None
    assert (await service.profile("alice"))["display_name"] == "Alice"

    sessions = SessionService(url)
    assert await sessions.load("unified:usr_hash", user_id="usr_hash", channel="telegram") is None
    moved = await sessions.load("unified:alice", user_id="alice", channel="telegram")
    assert moved["user_id"] == "alice"


async def test_touch_active_channel_records_when(url):
    service = IdentityService(url)
    await service.create_identity("usr_a", channel="telegram", channel_user_id="12345")
    assert (await service.profile("usr_a"))["active_channel"] is None

    await service.touch_active_channel("usr_a", "whatsapp")

    profile = await service.profile("usr_a")
    assert profile["active_channel"] == "whatsapp"
    # The same shape as SQLite's datetime('now'), which _is_within_age parses.
    assert len(profile["active_channel_updated_at"]) == 19


async def test_seeding_creates_identities_and_backfills_without_clobbering(url):
    service = IdentityService(url)
    # An identity that already exists, with a name the config does not set.
    await service.create_identity("alice", channel="telegram", channel_user_id="12345", display_name="Alice")

    await service.seed(
        [
            SeedEntry(
                pincer_user_id="alice",
                channels=[("telegram", "12345"), ("whatsapp", "4930111")],
                insert={"preferred_channel": "telegram", "display_name": None, "email": None, "timezone": None},
                backfill={"display_name": None, "preferred_channel": None, "email": "a@example.com", "timezone": None},
            ),
            SeedEntry(
                pincer_user_id="bob",
                channels=[("signal", "4930222")],
                insert={
                    "preferred_channel": "signal",
                    "display_name": "Bob",
                    "email": None,
                    "timezone": "Europe/Berlin",
                },
                backfill={"display_name": "Bob", "preferred_channel": None, "email": None, "timezone": None},
            ),
        ]
    )

    alice = await service.profile("alice")
    assert alice["display_name"] == "Alice"  # not overwritten by the config's None
    assert alice["email"] == "a@example.com"  # filled in
    assert await service.channels_for("alice") == [("telegram", "12345"), ("whatsapp", "4930111")]

    bob = await service.profile("bob")
    assert (bob["display_name"], bob["timezone"], bob["preferred_channel"]) == ("Bob", "Europe/Berlin", "signal")


async def test_prune_removes_unlisted_links_and_then_channelless_identities(url):
    service = IdentityService(url)
    await service.create_identity("usr_keep", channel="telegram", channel_user_id="12345")
    await service.create_identity("usr_drop", channel="telegram", channel_user_id="99999")
    await service.link_channel("usr_keep", "whatsapp", "4930111")

    unlisted, channelless = await service.prune([("telegram", "12345")])

    assert (unlisted, channelless) == (2, 1)
    assert await service.profile("usr_keep") is not None
    assert await service.profile("usr_drop") is None
    assert await service.channels_for("usr_keep") == [("telegram", "12345")]


async def test_listing_identities_for_the_dashboard(url):
    service = IdentityService(url)
    await service.create_identity("usr_a", channel="telegram", channel_user_id="12345", display_name="Alice")
    await service.create_identity("usr_b", channel="signal", channel_user_id="4930222")

    everyone = await service.list_profiles()
    assert {row["pincer_user_id"] for row in everyone} == {"usr_a", "usr_b"}
    alice = next(row for row in everyone if row["pincer_user_id"] == "usr_a")
    assert alice["channels"] == [
        {"channel": "telegram", "channel_user_id": "12345", "linked_at": alice["channels"][0]["linked_at"]}
    ]

    # Search matches a Pincer id or any channel id, and never duplicates a row.
    assert [row["pincer_user_id"] for row in await service.list_profiles(search="usr_a")] == ["usr_a"]
    assert [row["pincer_user_id"] for row in await service.list_profiles(search="4930222")] == ["usr_b"]
    assert await service.list_profiles(search="nothing") == []

    assert (await service.profile_with_channels("usr_b"))["channels"][0]["channel"] == "signal"
    assert await service.profile_with_channels("nobody") is None
