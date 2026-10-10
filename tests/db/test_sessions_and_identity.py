"""Sessions and identity on the repository/service layer, on both dialects.

They share a test module because a rename moves rows in both: an identity's
sessions follow it to its new id.
"""

from __future__ import annotations

import pytest

from pincer.db.engine import get_engine
from pincer.db.session import session_scope
from pincer.models.identity import AuthSession, ChannelIdentity, IdentityCredential, IdentityProfile
from pincer.models.sessions import ChatSession
from pincer.repositories.identity import (
    AuthSessionRepository,
    IdentityCredentialRepository,
    IdentityProfileRepository,
)
from pincer.services.identity import IdentityService, SeedEntry
from pincer.services.sessions import SessionService

_TABLES = [
    IdentityProfile.__table__,
    ChannelIdentity.__table__,
    IdentityCredential.__table__,
    AuthSession.__table__,
    ChatSession.__table__,
]


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


# ── credentials ──────────────────────────────────────────────────────

_NOW = "2026-10-10T12:00:00+00:00"


async def _set_password(url: str, pincer_user_id: str, password_hash: str) -> None:
    async with session_scope(url) as session:
        await IdentityCredentialRepository(session).set_password(pincer_user_id, password_hash, now=_NOW)


async def _credential(url: str, pincer_user_id: str) -> IdentityCredential | None:
    async with session_scope(url) as session:
        return await IdentityCredentialRepository(session).get(pincer_user_id)


async def test_setting_a_password_again_bumps_the_token_version(url):
    service = IdentityService(url)
    await service.create_identity("alice", channel="telegram", channel_user_id="12345")

    await _set_password(url, "alice", "hash-1")
    first = await _credential(url, "alice")
    assert first.password_hash == "hash-1"

    await _set_password(url, "alice", "hash-2")
    second = await _credential(url, "alice")
    assert (second.password_hash, second.token_version) == ("hash-2", first.token_version + 1)

    # A re-hash of the same password must not sign anyone out.
    async with session_scope(url) as session:
        await IdentityCredentialRepository(session).replace_password_hash("alice", "hash-3")
    third = await _credential(url, "alice")
    assert (third.password_hash, third.token_version) == ("hash-3", second.token_version)


async def test_a_recreated_credentials_row_does_not_resume_the_old_token_version(url):
    """Tokens are revoked by version. If a deleted row came back at the same
    version, every token from its first life would be valid again."""
    service = IdentityService(url)
    await service.create_identity("alice", channel="telegram", channel_user_id="12345")

    versions = set()
    for _ in range(5):
        await _set_password(url, "alice", "hash")
        versions.add((await _credential(url, "alice")).token_version)
        async with session_scope(url) as session:
            await IdentityCredentialRepository(session).delete_by_id("alice")
    assert len(versions) == 5

    async with session_scope(url) as session:
        await IdentityCredentialRepository(session).set_api_key(
            "alice", api_key_hash="h1", prefix="pnc_abcd", last4="wxyz", now=_NOW
        )
    assert (await _credential(url, "alice")).token_version not in versions


async def test_an_api_key_is_found_by_its_hash_and_keeps_the_password(url):
    service = IdentityService(url)
    await service.create_identity("alice", channel="telegram", channel_user_id="12345")
    await _set_password(url, "alice", "hash-1")

    async with session_scope(url) as session:
        repo = IdentityCredentialRepository(session)
        await repo.set_api_key("alice", api_key_hash="h1", prefix="pnc_abcd", last4="wxyz", now=_NOW)
    async with session_scope(url) as session:
        repo = IdentityCredentialRepository(session)
        found = await repo.by_api_key_hash("h1")
        assert (found.pincer_user_id, found.password_hash, found.api_key_last4) == ("alice", "hash-1", "wxyz")
        assert await repo.by_api_key_hash("h2") is None
        assert await repo.count_usable() == 1


async def test_renaming_moves_the_credentials(url):
    service = IdentityService(url)
    await service.create_identity("usr_hash", channel="telegram", channel_user_id="12345")
    await _set_password(url, "usr_hash", "hash-1")

    await service.rename_identity("usr_hash", "alice")

    assert await _credential(url, "usr_hash") is None
    assert (await _credential(url, "alice")).password_hash == "hash-1"


async def test_merging_keeps_the_target_s_credentials(url):
    service = IdentityService(url)
    await service.create_identity("usr_hash", channel="telegram", channel_user_id="12345")
    await service.create_identity("alice", channel="whatsapp", channel_user_id="4930111")
    await _set_password(url, "usr_hash", "hash-of-the-merged")
    await _set_password(url, "alice", "hash-of-alice")

    await service.rename_identity("usr_hash", "alice")

    assert await _credential(url, "usr_hash") is None
    assert (await _credential(url, "alice")).password_hash == "hash-of-alice"
    assert await service.channels_for("alice") == [("telegram", "12345"), ("whatsapp", "4930111")]


async def _open_session(url: str, pincer_user_id: str) -> str:
    async with session_scope(url) as session:
        row = await AuthSessionRepository(session).add(
            AuthSession(pincer_user_id=pincer_user_id, refresh_jti="jti", expires_at=4_000_000_000.0)
        )
        return row.id


async def _session_exists(url: str, session_id: str) -> bool:
    async with session_scope(url) as session:
        return await AuthSessionRepository(session).get(session_id) is not None


async def test_prune_keeps_an_identity_that_can_sign_in(url):
    """An identity made for the dashboard or an API key has no channel by
    design. Pruning it would lock its owner out at every restart."""
    service = IdentityService(url)
    await service.create_identity("usr_keep", channel="telegram", channel_user_id="12345")
    await service.create_identity("usr_unlisted", channel="telegram", channel_user_id="99999")
    await service.create_identity("usr_plain", channel="telegram", channel_user_id="55555")
    await service.create_profile("dashboard_admin")
    await service.create_profile("widget")
    await _set_password(url, "usr_unlisted", "hash")
    await _set_password(url, "dashboard_admin", "hash")
    async with session_scope(url) as session:
        await IdentityCredentialRepository(session).set_api_key(
            "widget", api_key_hash="h1", prefix="pnc_abcd", last4="wxyz", now=_NOW
        )
    signed_in = await _open_session(url, "dashboard_admin")

    unlisted, channelless = await service.prune([("telegram", "12345")])

    # Only the identity with neither a channel nor a credential goes.
    assert (unlisted, channelless) == (2, 1)
    assert await service.profile("usr_plain") is None
    for kept in ("usr_keep", "usr_unlisted", "dashboard_admin", "widget"):
        assert await service.profile(kept) is not None, kept
    assert (await _credential(url, "dashboard_admin")).password_hash == "hash"
    assert (await _credential(url, "usr_unlisted")).password_hash == "hash"
    assert await service.channels_for("usr_unlisted") == []  # the channel link itself is gone
    assert await _session_exists(url, signed_in)


async def test_prune_takes_an_identity_whose_credentials_were_revoked(url):
    service = IdentityService(url)
    await service.create_identity("usr_keep", channel="telegram", channel_user_id="12345")
    await service.create_profile("former_admin")
    await _set_password(url, "former_admin", "hash")
    session_id = await _open_session(url, "former_admin")
    assert (await service.prune([("telegram", "12345")]))[1] == 0

    async with session_scope(url) as session:
        await IdentityCredentialRepository(session).delete_by_id("former_admin")

    assert (await service.prune([("telegram", "12345")]))[1] == 1
    assert await service.profile("former_admin") is None
    assert not await _session_exists(url, session_id)  # no cascade on SQLite: swept explicitly


async def test_renaming_ends_the_sessions_of_the_old_id(url):
    service = IdentityService(url)
    await service.create_identity("usr_hash", channel="telegram", channel_user_id="12345")
    await _set_password(url, "usr_hash", "hash-1")
    session_id = await _open_session(url, "usr_hash")

    await service.rename_identity("usr_hash", "alice")

    assert not await _session_exists(url, session_id)
    assert (await _credential(url, "alice")).password_hash == "hash-1"


async def test_a_profile_made_by_hand_has_no_channel_and_is_found_by_email(url):
    service = IdentityService(url)
    assert await service.create_profile("alice", email="Alice@Example.com", display_name="Alice")
    assert not await service.create_profile("alice", email="other@example.com")
    assert await service.create_profile("bob", email="shared@example.com")
    assert await service.create_profile("carol", email="SHARED@example.com")

    assert (await service.profile("alice"))["email"] == "Alice@Example.com"
    assert await service.channels_for("alice") == []
    async with session_scope(url) as session:
        profiles = IdentityProfileRepository(session)
        assert [row.pincer_user_id for row in await profiles.find_by_email_ci(" alice@example.COM ")] == ["alice"]
        assert len(await profiles.find_by_email_ci("shared@example.com")) == 2
        assert await profiles.find_by_email_ci("nobody@example.com") == []
