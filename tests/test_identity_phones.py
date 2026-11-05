"""Identity phones (#299): TOML `phones`, seeding as confirmed, cleanup, and
the lookup a voice call uses to tell a registered caller from a guest."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from pincer.config.identity import load_identity_config, load_identity_map
from pincer.core.identity import IdentityResolver
from pincer.core.phone import normalize_e164
from pincer.db.session import session_scope
from pincer.repositories.identity import IdentityPhoneRepository

OWNER_TOML = """
[identity.owner]
phones = ["+49 151 2345-678", "+491512345678"]

[[identity.owner.channels]]
channel         = "telegram"
channel_user_id = 100100
"""


def _write(tmp_path: Path, text: str) -> None:
    (tmp_path / "pincer.toml").write_text(text)


async def _resolver(tmp_path: Path, mode: str = "update") -> IdentityResolver:
    resolver = IdentityResolver(tmp_path / "pincer.db", load_identity_map(tmp_path, mode), mode)
    await resolver.ensure_table()
    return resolver


def test_normalize_e164():
    assert normalize_e164("+49 (151) 234-5678") == "+491512345678"
    assert normalize_e164("491512345678") == "+491512345678"
    assert normalize_e164("0151 2345678") is None
    assert normalize_e164("") is None


# ── TOML ────────────────────────────────────────────────────────────


def test_phones_parsed_normalised_and_deduplicated(tmp_path):
    _write(tmp_path, OWNER_TOML)
    assert load_identity_config(tmp_path)["owner"].phones == ["+491512345678"]
    assert load_identity_map(tmp_path, "update")["owner"].phones == ["+491512345678"]


def test_phone_used_by_two_identities_is_an_error(tmp_path):
    _write(
        tmp_path,
        OWNER_TOML
        + """
[identity.other]
phones = ["491512345678"]
[[identity.other.channels]]
channel         = "telegram"
channel_user_id = 200200
""",
    )
    with pytest.raises(ValueError, match="already used by identity.owner"):
        load_identity_config(tmp_path)


@pytest.mark.parametrize("phones", ['"+491512345678"', '["not a number"]'])
def test_invalid_phones_are_an_error(tmp_path, phones):
    _write(
        tmp_path,
        f'[identity.owner]\nphones = {phones}\n[[identity.owner.channels]]\nchannel = "telegram"\n'
        "channel_user_id = 1\n",
    )
    with pytest.raises(ValueError, match="phone"):
        load_identity_config(tmp_path)


# ── Seeding, lookup, cleanup ────────────────────────────────────────


@pytest.mark.asyncio
async def test_seeded_phone_is_confirmed_and_resolves(tmp_path):
    _write(tmp_path, OWNER_TOML)
    resolver = await _resolver(tmp_path)
    await resolver.seed_from_config()
    await resolver.cleanup()
    store = resolver._store
    assert await store.user_for_confirmed_phone("+49 151 2345678") == "owner"
    assert await store.user_for_confirmed_phone("+4930000000") is None
    profile = await store.profile_with_channels("owner")
    assert profile is not None
    assert profile["phones"] == [{"phone": "+491512345678", "confirmed": True}]
    # Seeding again keeps the first confirmation time
    async with session_scope(store._url) as session:
        first = (await IdentityPhoneRepository(session).get("+491512345678")).confirmed_at
    await resolver.seed_from_config()
    async with session_scope(store._url) as session:
        row = await IdentityPhoneRepository(session).get("+491512345678")
    assert row.confirmed_at == first and row.source == "config"


@pytest.mark.asyncio
async def test_unconfirmed_phone_is_a_guest(tmp_path):
    _write(tmp_path, OWNER_TOML)
    resolver = await _resolver(tmp_path)
    await resolver.seed_from_config()
    store = resolver._store
    async with session_scope(store._url) as session:
        await IdentityPhoneRepository(session).upsert(
            "+4930123456", "owner", confirmed=False, source="api", now=datetime.now(UTC).isoformat()
        )
    assert await store.user_for_confirmed_phone("+4930123456") is None
    profile = await store.profile_with_channels("owner")
    assert {"phone": "+4930123456", "confirmed": False} in profile["phones"]


@pytest.mark.asyncio
async def test_cleanup_removes_config_phones_no_longer_listed(tmp_path):
    _write(tmp_path, OWNER_TOML)
    resolver = await _resolver(tmp_path)
    await resolver.seed_from_config()
    store = resolver._store
    async with session_scope(store._url) as session:
        await IdentityPhoneRepository(session).upsert(
            "+4930123456", "owner", confirmed=False, source="api", now=datetime.now(UTC).isoformat()
        )

    _write(tmp_path, OWNER_TOML.replace('phones = ["+49 151 2345-678", "+491512345678"]', ""))
    resolver = await _resolver(tmp_path)
    await resolver.cleanup_phones()
    assert await store.user_for_confirmed_phone("+491512345678") is None
    async with session_scope(store._url) as session:
        phones = await IdentityPhoneRepository(session).phones_for("owner")
    assert phones == [("+4930123456", False)]  # not from the config: left alone


@pytest.mark.asyncio
async def test_cleanup_keeps_phones_still_in_the_config(tmp_path):
    two = OWNER_TOML.replace('["+49 151 2345-678", "+491512345678"]', '["+491512345678", "+491700000001"]')
    _write(tmp_path, two)
    resolver = await _resolver(tmp_path)
    await resolver.seed_from_config()
    store = resolver._store

    _write(tmp_path, OWNER_TOML)  # +491700000001 removed, +491512345678 kept
    resolver = await _resolver(tmp_path)
    await resolver.cleanup_phones()
    assert await store.user_for_confirmed_phone("+491512345678") == "owner"
    assert await store.user_for_confirmed_phone("+491700000001") is None


@pytest.mark.asyncio
async def test_removed_phone_is_dropped_even_when_seeding_fails(tmp_path, monkeypatch):
    from pincer.channels.router import ChannelRouter

    _write(tmp_path, OWNER_TOML)
    resolver = await _resolver(tmp_path)
    await resolver.seed_from_config()
    store = resolver._store

    _write(tmp_path, OWNER_TOML.replace('phones = ["+49 151 2345-678", "+491512345678"]', ""))
    resolver = await _resolver(tmp_path)

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("channel resolution failed")

    monkeypatch.setattr(resolver, "seed_from_config", _boom)
    await ChannelRouter(resolver).rebuild_identity_map()
    assert await store.user_for_confirmed_phone("+491512345678") is None


@pytest.mark.asyncio
async def test_listing_identities_includes_phones(tmp_path):
    _write(tmp_path, OWNER_TOML)
    resolver = await _resolver(tmp_path)
    await resolver.seed_from_config()
    rows = await resolver._store.list_profiles()
    assert rows[0]["phones"] == [{"phone": "+491512345678", "confirmed": True}]


@pytest.mark.asyncio
async def test_rename_carries_phones_and_prune_drops_orphans(tmp_path):
    _write(tmp_path, OWNER_TOML)
    resolver = await _resolver(tmp_path)
    await resolver.seed_from_config()
    store = resolver._store

    await store.rename_identity("owner", "boss")
    assert await store.user_for_confirmed_phone("+491512345678") == "boss"

    # The identity loses its only channel: prune takes the profile and its phone.
    removed, orphaned = await store.prune(set())
    assert (removed, orphaned) == (1, 1)
    async with session_scope(store._url) as session:
        assert await IdentityPhoneRepository(session).get("+491512345678") is None


OTHER_TOML = """
[identity.other]
phones = ["+491700000002"]

[[identity.other.channels]]
channel         = "telegram"
channel_user_id = 200200
"""


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "other_kept"), [("sync", False), ("update", True), ("clionly", True)])
async def test_phone_cleanup_follows_the_mode(tmp_path, mode, other_kept):
    """#348: sync drops every config phone the TOML lost; update only those of
    the people it still lists; clionly none."""
    _write(tmp_path, OWNER_TOML + OTHER_TOML)
    resolver = await _resolver(tmp_path, "sync")
    await resolver.seed_from_config()
    store = resolver._store

    # `other` leaves the TOML; `owner` stays but loses its phone.
    _write(tmp_path, OWNER_TOML.replace('phones = ["+49 151 2345-678", "+491512345678"]', ""))
    await (await _resolver(tmp_path, mode)).cleanup_phones()

    assert (await store.user_for_confirmed_phone("+491700000002") == "other") is other_kept
    owner_kept = mode == "clionly"
    assert (await store.user_for_confirmed_phone("+491512345678") == "owner") is owner_kept


@pytest.mark.asyncio
async def test_a_deactivated_owner_is_a_guest_on_calls(tmp_path):
    _write(tmp_path, OWNER_TOML)
    resolver = await _resolver(tmp_path)
    await resolver.seed_from_config()
    store = resolver._store
    await store.set_active("owner", False)
    assert await store.user_for_confirmed_phone("+491512345678") is None
    # Seeding again keeps it off.
    await resolver.seed_from_config()
    assert await store.user_for_confirmed_phone("+491512345678") is None
    await store.set_active("owner", True)
    assert await store.user_for_confirmed_phone("+491512345678") == "owner"
