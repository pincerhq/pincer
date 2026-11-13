"""Sessions and identity on the repository/service layer, on both dialects.

They share a test module because a rename moves rows in both: an identity's
sessions follow it to its new id.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pincer.core.session import SessionManager
from pincer.db.engine import get_engine
from pincer.db.session import session_scope
from pincer.llm.base import LLMMessage, MessageRole
from pincer.models.identity import AuthSession, ChannelIdentity, IdentityCredential, IdentityPhone, IdentityProfile
from pincer.models.onboarding import Onboarding
from pincer.models.sessions import MEMORY_FREE_KEY, ChatSession
from pincer.repositories.identity import (
    AuthSessionRepository,
    IdentityCredentialRepository,
    IdentityPhoneRepository,
    IdentityProfileRepository,
)
from pincer.repositories.sessions import SessionRepository, rekeyed_session_id
from pincer.services.identity import IdentityService, SeedEntry
from pincer.services.sessions import SessionService

_TABLES = [
    IdentityProfile.__table__,
    ChannelIdentity.__table__,
    IdentityCredential.__table__,
    IdentityPhone.__table__,
    AuthSession.__table__,
    ChatSession.__table__,
    Onboarding.__table__,
]

_NOW = "2026-10-10T12:00:00+00:00"
_CONVERSATION = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
async def url(db_url: str):
    engine = get_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: IdentityProfile.metadata.drop_all(c, tables=_TABLES))
        await conn.run_sync(lambda c: IdentityProfile.metadata.create_all(c, tables=_TABLES))
    yield db_url
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: IdentityProfile.metadata.drop_all(c, tables=_TABLES))


async def _store_session(
    url: str,
    session_id: str,
    *,
    user_id: str,
    channel: str = "telegram",
    at: float = 1.0,
    messages: str = "[]",
    metadata: str = "{}",
    created_at: float | None = None,
):
    """A stored row, updated `at` and created then too unless `created_at` says otherwise."""
    await SessionService(url).save(
        session_id=session_id,
        user_id=user_id,
        channel=channel,
        messages=messages,
        metadata=metadata,
        created_at=at if created_at is None else created_at,
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


async def test_the_legacy_fallback_never_adopts_a_conversation_nested_under_the_key(url):
    """A user whose web traffic has all gone through conversations has no
    main session yet; the main session must start empty, not continue one
    of the conversations."""
    await _store_session(url, f"web:u:{_CONVERSATION}", user_id="u", channel="web")

    row = await SessionService(url).load("web:u", user_id="u", channel="web")
    assert row is None, f"adopted {row['session_id']}"


async def test_the_legacy_fallback_still_finds_a_legacy_row_beside_newer_conversations(url):
    """Only the requested key's own conversations are skipped: a session left
    under an older key (here an identity merged into `u`) is still the main
    session, even when a conversation was updated after it."""
    await _store_session(url, "web:old", user_id="u", channel="web", at=5.0)
    await _store_session(url, f"web:u:{_CONVERSATION}", user_id="u", channel="web", at=10.0)

    row = await SessionService(url).load("web:u", user_id="u", channel="web")
    assert row is not None
    assert row["session_id"] == "web:old"


async def test_the_nested_exclusion_escapes_like_wildcards(url):
    """`_` in a user id is a literal, not LIKE's any-character: a session
    that only matches `web:u_1:` as a wildcard is not one of u_1's
    conversations, so the fallback still finds it."""
    await _store_session(url, "web:uX1:thread", user_id="u_1", channel="web")

    row = await SessionService(url).load("web:u_1", user_id="u_1", channel="web")
    assert row is not None, "web:uX1:thread was excluded as if `_` were a wildcard"
    assert row["session_id"] == "web:uX1:thread"


@pytest.mark.parametrize("spelling", ["WEB:U", "web:U"])
async def test_the_nested_exclusion_is_case_sensitive(url, spelling):
    """Only `web:u:` itself is a conversation of `web:u`; a prefix that
    differs in case is not, on either dialect (SQLite's LIKE folds case)."""
    await _store_session(url, f"{spelling}:{_CONVERSATION}", user_id="u", channel="web")

    row = await SessionService(url).load("web:u", user_id="u", channel="web")
    assert row is not None, f"{spelling}:… was excluded as if it were web:u:…"
    assert row["session_id"] == f"{spelling}:{_CONVERSATION}"


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


async def _session_ids(url: str) -> set[str]:
    async with session_scope(url) as session:
        return set(await SessionRepository(session).ids())


async def _say(url: str, user_id: str, channel: str, text: str) -> None:
    """Store a message the way the main message path does."""
    # Attached to `url` directly rather than through `initialize()`, which
    # only takes a SQLite path, so this runs on either dialect.
    manager = SessionManager(Path(), max_messages=20)
    manager._service = SessionService(url)
    session = await manager.get_or_create(user_id, channel)
    await manager.add_message(session, LLMMessage(role=MessageRole.USER, content=text))


async def _messages(url: str, session_id: str) -> str:
    row = await SessionService(url).load(session_id, user_id="", channel="", legacy_fallback=False)
    assert row is not None
    return row["messages"]


@pytest.mark.parametrize(
    ("source_at", "target_at", "joined"),
    [
        (100.0, 200.0, ["from usr_hash", "from alice"]),
        (200.0, 100.0, ["from alice", "from usr_hash"]),
        (100.0, 100.0, ["from alice", "from usr_hash"]),  # the target first on a tie
    ],
)
async def test_merging_joins_colliding_channel_sessions(url, source_at, target_at, joined):
    """Neither id's conversation is dropped: both land in the target, oldest first."""
    service = IdentityService(url)
    await service.create_identity("usr_hash", channel="telegram", channel_user_id="12345")
    await service.create_identity("alice", channel="whatsapp", channel_user_id="4930111")
    await _store_session(url, "whatsapp:usr_hash", user_id="usr_hash", at=source_at, messages='["from usr_hash"]')
    await _store_session(url, "whatsapp:alice", user_id="alice", at=target_at, messages='["from alice"]')

    await service.rename_identity("usr_hash", "alice")

    assert await _session_ids(url) == {"whatsapp:alice"}
    assert json.loads(await _messages(url, "whatsapp:alice")) == joined


async def test_a_merge_is_not_undone_by_a_cached_session(url):
    """#274 through the message path: both halves of a conversation were said
    and saved by a running `SessionManager`, which still caches them. The next
    turn after the merge continues the merged row instead of saving its stale
    copy over it, and the source's key does not come back."""
    service = IdentityService(url)
    await service.create_identity("usr_hash", channel="web", channel_user_id="12345")
    await service.create_identity("alice", channel="telegram", channel_user_id="4930111")
    manager = SessionManager(Path(), max_messages=20)
    manager._service = SessionService(url)
    source, target = f"web:usr_hash:{_CONVERSATION}", f"web:alice:{_CONVERSATION}"
    for user_id, key, text in [("usr_hash", source, "anonymous half"), ("alice", target, "signed-in half")]:
        session = await manager.get_or_create(user_id, "web", session_key=key)
        await manager.add_message(session, LLMMessage(role=MessageRole.USER, content=text))

    await service.rename_identity("usr_hash", "alice")
    session = await manager.get_or_create("alice", "web", session_key=target)
    await manager.add_message(session, LLMMessage(role=MessageRole.USER, content="next"))
    await manager.close()

    assert await _session_ids(url) == {target}
    stored = [m["content"] for m in json.loads(await _messages(url, target))]
    assert stored == ["anonymous half", "signed-in half", "next"]


@pytest.mark.parametrize("source_newer", [True, False])
async def test_merging_combines_metadata_and_spans_both_times(url, source_newer):
    """The more recently updated row's metadata wins a shared key; the other
    row's own keys are kept. The merged row spans both rows' times."""
    service = IdentityService(url)
    await service.create_identity("usr_hash", channel="telegram", channel_user_id="12345")
    await service.create_identity("alice", channel="whatsapp", channel_user_id="4930111")
    spans = [(100.0, 400.0), (200.0, 300.0)]
    (source_created, source_at), (target_created, target_at) = spans if source_newer else spans[::-1]
    await _store_session(
        url,
        "whatsapp:usr_hash",
        user_id="usr_hash",
        created_at=source_created,
        at=source_at,
        metadata=json.dumps({"flag": "source", "only_source": 1}),
    )
    await _store_session(
        url,
        "whatsapp:alice",
        user_id="alice",
        created_at=target_created,
        at=target_at,
        metadata=json.dumps({"flag": "target", "only_target": 2}),
    )

    await service.rename_identity("usr_hash", "alice")

    row = await SessionService(url).load("whatsapp:alice", user_id="", channel="", legacy_fallback=False)
    assert row is not None
    assert json.loads(row["metadata"]) == {
        "flag": "source" if source_newer else "target",
        "only_source": 1,
        "only_target": 2,
    }
    assert row["created_at"] == 100.0
    assert row["updated_at"] == 400.0


@pytest.mark.parametrize("memory_free_side", ["usr_hash", "alice"])
async def test_merging_a_memory_free_conversation_keeps_it_memory_free(url, memory_free_side):
    """What was said off the record stays off it, whichever side said it."""
    service = IdentityService(url)
    await service.create_identity("usr_hash", channel="web", channel_user_id="12345")
    await service.create_identity("alice", channel="telegram", channel_user_id="4930111")
    for user_id, at in [("usr_hash", 100.0), ("alice", 200.0)]:
        metadata = {MEMORY_FREE_KEY: "true"} if user_id == memory_free_side else {}
        key = f"web:{user_id}:{_CONVERSATION}"
        await _store_session(url, key, user_id=user_id, channel="web", at=at, metadata=json.dumps(metadata))

    await service.rename_identity("usr_hash", "alice")

    row = await SessionService(url).load(f"web:alice:{_CONVERSATION}", user_id="", channel="", legacy_fallback=False)
    assert row is not None
    assert json.loads(row["metadata"]) == {MEMORY_FREE_KEY: "true"}


@pytest.mark.parametrize(
    ("messages", "metadata"), [("not json", "not json"), ("null", "null"), ('{"a": 1}', '["a"]'), ("", "")]
)
async def test_merging_drops_a_side_whose_stored_content_is_unreadable(url, messages, metadata):
    """A corrupt row must not roll back the whole rename: the readable side is kept."""
    service = IdentityService(url)
    await service.create_identity("usr_hash", channel="telegram", channel_user_id="12345")
    await service.create_identity("alice", channel="whatsapp", channel_user_id="4930111")
    await _store_session(url, "whatsapp:usr_hash", user_id="usr_hash", at=100.0, messages=messages, metadata=metadata)
    await _store_session(url, "whatsapp:alice", user_id="alice", at=200.0, messages='["kept"]')

    await service.rename_identity("usr_hash", "alice")

    assert await _session_ids(url) == {"whatsapp:alice"}
    row = await SessionService(url).load("whatsapp:alice", user_id="", channel="", legacy_fallback=False)
    assert row is not None
    assert json.loads(row["messages"]) == ["kept"]
    assert json.loads(row["metadata"]) == {}


async def test_merging_joins_main_and_nested_collisions_in_one_rename(url):
    service = IdentityService(url)
    await service.create_identity("usr_hash", channel="web", channel_user_id="12345")
    await service.create_identity("alice", channel="telegram", channel_user_id="4930111")
    nested_source, nested_target = f"web:usr_hash:{_CONVERSATION}", f"web:alice:{_CONVERSATION}"
    await _store_session(url, "web:usr_hash", user_id="usr_hash", channel="web", at=100.0, messages='["main source"]')
    await _store_session(url, "web:alice", user_id="alice", channel="web", at=200.0, messages='["main target"]')
    await _store_session(url, nested_source, user_id="usr_hash", channel="web", at=100.0, messages='["nested source"]')
    await _store_session(url, nested_target, user_id="alice", channel="web", at=200.0, messages='["nested target"]')

    await service.rename_identity("usr_hash", "alice")

    assert await _session_ids(url) == {"web:alice", nested_target}
    assert json.loads(await _messages(url, "web:alice")) == ["main source", "main target"]
    assert json.loads(await _messages(url, nested_target)) == ["nested source", "nested target"]


async def test_rewrite_session_ids_counts_merges_and_moves(url):
    """A merge updates the target and removes the source (2); a move is 1."""
    await _store_session(url, "whatsapp:usr_hash", user_id="usr_hash", at=100.0, messages='["a"]')
    await _store_session(url, "whatsapp:alice", user_id="alice", at=200.0, messages='["b"]')
    await _store_session(url, "telegram:usr_hash", user_id="usr_hash", at=100.0)

    async with session_scope(url) as session:
        changed = await SessionRepository(session).rewrite_session_ids("usr_hash", "alice")

    assert changed == 3
    assert await _session_ids(url) == {"whatsapp:alice", "telegram:alice"}


async def test_renaming_moves_only_keys_ending_in_the_identity(url):
    """A key moves when its last segment is exactly the old id. A call's
    `voice:` key never does, and a key merely containing the id stays."""
    service = IdentityService(url)
    await service.create_identity("usr_hash", channel="telegram", channel_user_id="12345")
    moved = {
        "telegram:usr_hash": "telegram:alice",
        "unified:usr_hash": "unified:alice",
        "teams-chat-19:abc@thread.v2:usr_hash": "teams-chat-19:abc@thread.v2:alice",
    }
    untouched = {"voice:usr_hash", "voice:CAusr_hash01", "unified:usr_hash_2", "unified:xusr_hash", "telegram:usrXhash"}
    for session_id in [*moved, *untouched]:
        await _store_session(url, session_id, user_id="usr_hash")

    await service.rename_identity("usr_hash", "alice")

    assert await _session_ids(url) == set(moved.values()) | untouched


@pytest.mark.parametrize(
    ("session_id", "moved"),
    [
        ("web:usr_ha", "web:bob"),
        (f"web:usr_ha:{_CONVERSATION}", f"web:bob:{_CONVERSATION}"),
        ("teams-chat-19:abc@thread.v2:usr_ha", "teams-chat-19:abc@thread.v2:bob"),
        ("web:usr_hash", None),
        (f"web:usr_hash:{_CONVERSATION}", None),
        (f"web:xusr_ha:{_CONVERSATION}", None),
        # The id appears as a whole segment, but not as the user's: the prefix holds it.
        (f"teams:usr_ha:usr_hash:{_CONVERSATION}", None),
        ("web:usr_ha:not-a-conversation", None),
        ("web:usr_ha:2222AAAA-2222-4222-8222-222222222222", None),  # not the canonical spelling
        ("voice:usr_ha", None),
        (f"voice:usr_ha:{_CONVERSATION}", None),
    ],
)
def test_a_rename_moves_a_key_only_where_the_id_is_its_user_segment(session_id, moved):
    assert rekeyed_session_id(session_id, "usr_ha", "bob") == moved


async def test_renaming_moves_conversations_nested_under_the_identity(url):
    """A web chat conversation (`web:<id>:<conversation>`) follows its user."""
    service = IdentityService(url)
    await service.create_identity("usr_hash", channel="web", channel_user_id="12345")
    await _store_session(url, "web:usr_hash", user_id="usr_hash", channel="web")
    await _store_session(url, f"web:usr_hash:{_CONVERSATION}", user_id="usr_hash", channel="web")

    await service.rename_identity("usr_hash", "alice")

    assert await _session_ids(url) == {"web:alice", f"web:alice:{_CONVERSATION}"}


@pytest.mark.parametrize(
    ("source_at", "target_at", "joined"),
    [(100.0, 200.0, ["source", "target"]), (200.0, 100.0, ["target", "source"])],
)
async def test_merging_joins_a_nested_conversation(url, source_at, target_at, joined):
    """#274: a visitor chats in a conversation, signs in and keeps it, and the
    signed-in id gets its own row for it. The merge keeps both halves."""
    service = IdentityService(url)
    await service.create_identity("usr_hash", channel="web", channel_user_id="12345")
    await service.create_identity("alice", channel="telegram", channel_user_id="4930111")
    await _store_session(
        url, f"web:usr_hash:{_CONVERSATION}", user_id="usr_hash", channel="web", at=source_at, messages='["source"]'
    )
    await _store_session(
        url, f"web:alice:{_CONVERSATION}", user_id="alice", channel="web", at=target_at, messages='["target"]'
    )

    await service.rename_identity("usr_hash", "alice")

    assert await _session_ids(url) == {f"web:alice:{_CONVERSATION}"}
    assert json.loads(await _messages(url, f"web:alice:{_CONVERSATION}")) == joined


async def test_renaming_moves_nested_conversations_only_for_the_whole_id(url):
    """`usr_ha` is not `usr_hash`, and only a conversation id makes a key nested."""
    service = IdentityService(url)
    await service.create_identity("usr_ha", channel="web", channel_user_id="12345")
    untouched = {
        f"web:usr_hash:{_CONVERSATION}",
        f"web:xusr_ha:{_CONVERSATION}",
        "web:usr_ha:not-a-conversation",
        f"voice:usr_ha:{_CONVERSATION}",
    }
    for session_id in untouched:
        await _store_session(url, session_id, user_id="usr_hash", channel="web")
    await _store_session(url, f"web:usr_ha:{_CONVERSATION}", user_id="usr_ha", channel="web")

    await service.rename_identity("usr_ha", "bob")

    assert await _session_ids(url) == untouched | {f"web:bob:{_CONVERSATION}"}


async def test_touch_active_channel_records_when(url):
    service = IdentityService(url)
    await service.create_identity("usr_a", channel="telegram", channel_user_id="12345")
    assert (await service.profile("usr_a"))["active_channel"] is None

    await service.touch_active_channel("usr_a", "whatsapp")

    profile = await service.profile("usr_a")
    assert profile["active_channel"] == "whatsapp"
    # The same shape as SQLite's datetime('now'), which _is_within_age parses.
    assert len(profile["active_channel_updated_at"]) == 19


async def test_seeding_creates_identities_and_overwrites_only_what_the_config_states(url):
    service = IdentityService(url)
    # An identity that already exists, with a name the config does not set
    # and an email the config changes.
    await service.create_identity("alice", channel="telegram", channel_user_id="12345", display_name="Alice")
    await service.update_profile("alice", {"email": "old@example.com"})

    await service.seed(
        [
            SeedEntry(
                pincer_user_id="alice",
                channels=[("telegram", "12345"), ("whatsapp", "4930111")],
                insert={"preferred_channel": "telegram", "display_name": None, "email": None, "timezone": None},
                overwrite={"display_name": None, "preferred_channel": None, "email": "a@example.com", "timezone": None},
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
                overwrite={"display_name": "Bob", "preferred_channel": None, "email": None, "timezone": None},
            ),
        ]
    )

    alice = await service.profile("alice")
    assert alice["display_name"] == "Alice"  # not overwritten by the config's None
    assert alice["email"] == "a@example.com"  # the config's value wins
    assert (alice["source"], alice["active"]) == ("config", True)
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
    await service.create_profile("former_admin", source="config")
    await _set_password(url, "former_admin", "hash")
    session_id = await _open_session(url, "former_admin")
    assert (await service.prune([("telegram", "12345")]))[1] == 0

    async with session_scope(url) as session:
        await IdentityCredentialRepository(session).delete_by_id("former_admin")

    assert (await service.prune([("telegram", "12345")]))[1] == 1
    assert await service.profile("former_admin") is None
    assert not await _session_exists(url, session_id)  # no cascade on SQLite: swept explicitly


async def test_sync_prune_leaves_cli_identities_alone(url):
    """#348: what `pincer identity` made is not the TOML's to remove."""
    service = IdentityService(url)
    await service.create_profile("handmade", channels=[("telegram", "777")])
    await service.create_profile("bare")  # no channel, no credential
    await service.create_identity("usr_auto", channel="telegram", channel_user_id="888")
    await service.create_identity("config_person", channel="telegram", channel_user_id="999")

    unlisted, channelless = await service.prune([("telegram", "12345")])

    assert (unlisted, channelless) == (2, 2)
    assert await service.channels_for("handmade") == [("telegram", "777")]
    assert await service.profile("bare") is not None
    assert await service.profile("usr_auto") is None
    assert await service.profile("config_person") is None


async def test_update_prune_unlinks_only_listed_identities_and_deletes_nobody(url):
    service = IdentityService(url)
    await service.create_identity("alice", channel="telegram", channel_user_id="1")
    await service.link_channel("alice", "slack", "U1")
    await service.create_identity("bob", channel="telegram", channel_user_id="2")

    assert await service.prune([("telegram", "1")], listed={"alice"}) == (1, 0)
    assert await service.channels_for("alice") == [("telegram", "1")]
    assert await service.channels_for("bob") == [("telegram", "2")]


async def test_deactivating_ends_sessions_keeps_credentials_and_hides_the_identity(url):
    service = IdentityService(url)
    await service.create_identity("alice", channel="telegram", channel_user_id="12345")
    await _set_password(url, "alice", "hash")
    await service.add_phone("alice", "+4915100000009")
    session_id = await _open_session(url, "alice")
    assert await service.has_roster()

    assert await service.set_active("alice", False)

    assert not await _session_exists(url, session_id)
    assert (await _credential(url, "alice")).password_hash == "hash"
    assert await service.user_for_channel("telegram", "12345") is None
    assert await service.owner_any("telegram", "12345") == ("alice", False)
    assert await service.user_for_confirmed_phone("+4915100000009") is None
    assert await service.has_roster()  # a deactivated person is a guest, not a reason to open the gate
    assert (await service.profile_with_channels("alice"))["active"] is False

    assert await service.set_active("alice", True)
    assert await service.user_for_channel("telegram", "12345") == "alice"
    assert await service.user_for_confirmed_phone("+4915100000009") == "alice"
    assert not await service.set_active("nobody", False)


async def test_auto_identities_do_not_make_a_roster(url):
    """An ungated deployment makes `usr_` ids for every sender; counting them
    would close the gate on everyone after the first message."""
    service = IdentityService(url)
    await service.create_identity("usr_a", channel="telegram", channel_user_id="1")
    assert not await service.has_roster()
    await service.create_profile("bob", channels=[("telegram", "2")], password_hash="h")
    assert await service.has_roster()


async def test_channel_and_phone_conflicts_are_refused(url):
    from pincer.services.identity import ChannelTakenError, PhoneTakenError

    service = IdentityService(url)
    await service.create_identity("alice", channel="telegram", channel_user_id="1")
    await service.add_phone("alice", "+4915100000001")
    with pytest.raises(ChannelTakenError):
        await service.create_profile("bob", channels=[("telegram", "1")])
    assert await service.profile("bob") is None  # nothing left behind
    await service.create_profile("bob", channels=[("telegram", "2")])
    with pytest.raises(ChannelTakenError):
        await service.add_channel("bob", "telegram", "1")
    assert not await service.add_channel("bob", "telegram", "2")  # already its own
    with pytest.raises(PhoneTakenError):
        await service.add_phone("bob", "+4915100000001")
    assert not await service.unlink_channel("bob", "telegram", "1")
    assert await service.unlink_channel("bob", "telegram", "2")
    assert await service.remove_phone("alice", "+4915100000001")


async def test_renaming_keeps_status_and_source(url):
    service = IdentityService(url)
    await service.create_identity("usr_hash", channel="telegram", channel_user_id="12345")
    await service.set_active("usr_hash", False)
    await service.rename_identity("usr_hash", "alice")
    alice = await service.profile("alice")
    assert (alice["active"], alice["source"]) == (False, "auto")


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


async def test_rotating_a_session_needs_the_refresh_token_that_is_current(url):
    """Compare-and-set: of two refreshes holding the same token, the second
    one's swap matches no row — on either dialect."""
    service = IdentityService(url)
    await service.create_identity("alice", channel="telegram", channel_user_id="12345")
    await _set_password(url, "alice", "hash")
    async with session_scope(url) as session:
        row = AuthSession(pincer_user_id="alice", refresh_jti="r0", expires_at=4_000_000_000.0)
        await AuthSessionRepository(session).add(row, refresh=False)
        session_id = row.id

    async with session_scope(url) as session:
        sessions = AuthSessionRepository(session)
        assert await sessions.rotate(session_id, "r0", {"refresh_jti": "r1", "prev_refresh_jti": "r0"}) == 1
        assert await sessions.rotate(session_id, "r0", {"refresh_jti": "r2", "prev_refresh_jti": "r0"}) == 0
        assert await sessions.rotate(session_id, "never-issued", {"refresh_jti": "r3"}) == 0

    async with session_scope(url) as session:
        stored = await AuthSessionRepository(session).get(session_id)
        assert (stored.refresh_jti, stored.prev_refresh_jti) == ("r1", "r0")


# ── Identity phones (#299) ───────────────────────────────────────────


def _phone_seed(uid: str, channel_id: str, phones: tuple[str, ...]) -> SeedEntry:
    fields = {"preferred_channel": "telegram", "display_name": None, "email": None, "timezone": None}
    return SeedEntry(
        pincer_user_id=uid,
        channels=[("telegram", channel_id)],
        insert=fields,
        overwrite={**fields, "preferred_channel": None},
        phones=phones,
    )


async def _phone_row(url: str, phone: str):
    async with session_scope(url) as session:
        return await IdentityPhoneRepository(session).get(phone)


async def test_seeding_stores_config_phones_confirmed(url):
    service = IdentityService(url)
    await service.seed([_phone_seed("alice", "111", ("+4915112345678",))])
    assert (await service.profile_with_channels("alice"))["phones"] == [{"phone": "+4915112345678", "confirmed": True}]
    assert await service.user_for_confirmed_phone("+49 151 12345678") == "alice"
    first = await _phone_row(url, "+4915112345678")
    assert first is not None and first.source == "config" and first.confirmed_at is not None

    # Seeding again (every start) takes the conflict path and keeps the first confirmation
    await service.seed([_phone_seed("alice", "111", ("+4915112345678",))])
    again = await _phone_row(url, "+4915112345678")
    assert again is not None and again.confirmed_at == first.confirmed_at


async def test_an_unconfirmed_phone_has_no_owner(url):
    service = IdentityService(url)
    await service.seed([_phone_seed("alice", "111", ())])
    async with session_scope(url) as session:
        await IdentityPhoneRepository(session).upsert("+4930123456", "alice", confirmed=False, source="api", now=_NOW)
    assert await service.user_for_confirmed_phone("+4930123456") is None
    assert (await service.profile_with_channels("alice"))["phones"] == [{"phone": "+4930123456", "confirmed": False}]


async def test_pruning_config_phones_keeps_listed_and_non_config_ones(url):
    service = IdentityService(url)
    await service.seed([_phone_seed("alice", "111", ("+4915100000001", "+4915100000002"))])
    async with session_scope(url) as session:
        await IdentityPhoneRepository(session).upsert("+4930123456", "alice", confirmed=False, source="api", now=_NOW)

    assert await service.prune_config_phones(["+4915100000001"]) == 1
    assert await service.user_for_confirmed_phone("+4915100000001") == "alice"
    assert await service.user_for_confirmed_phone("+4915100000002") is None
    assert await _phone_row(url, "+4930123456") is not None


async def test_listing_identities_batches_their_phones(url):
    service = IdentityService(url)
    await service.seed([_phone_seed("alice", "111", ("+4915100000001",)), _phone_seed("bob", "222", ())])
    rows = {row["pincer_user_id"]: row["phones"] for row in await service.list_profiles()}
    assert rows == {"alice": [{"phone": "+4915100000001", "confirmed": True}], "bob": []}


async def test_renaming_moves_phones_and_pruning_drops_them(url):
    service = IdentityService(url)
    await service.seed([_phone_seed("alice", "111", ("+4915100000001",))])
    await service.rename_identity("alice", "alicia")
    assert await service.user_for_confirmed_phone("+4915100000001") == "alicia"

    assert await service.prune(set()) == (1, 1)
    assert await _phone_row(url, "+4915100000001") is None


async def test_confirmed_phone_owners_count_cli_phones_of_active_identities(url):
    service = IdentityService(url)
    await service.create_profile("alice", channels=[("telegram", "1")])
    await service.create_profile("bob", channels=[("telegram", "2")])
    await service.add_phone("alice", "+4915100000001")
    await service.add_phone("bob", "+4915100000002")
    async with session_scope(url) as session:
        await IdentityPhoneRepository(session).upsert("+4930123456", "bob", confirmed=False, source="api", now=_NOW)
    assert await service.confirmed_phone_owners() == ["alice", "bob"]

    await service.set_active("bob", False)
    assert await service.confirmed_phone_owners() == ["alice"]
