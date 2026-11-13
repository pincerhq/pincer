"""The photos of a conversation, on the repository/service layer, on both dialects (issue #358, slice 3).

They share a module with the identity tables because a merge moves photos with their sessions.
"""

from __future__ import annotations

import base64
import json
import logging
import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlmodel import col

from pincer.core.session import Session, SessionManager
from pincer.db.engine import get_engine
from pincer.db.session import session_scope
from pincer.llm.base import ImageContent, LLMMessage, MessageRole
from pincer.models.identity import AuthSession, ChannelIdentity, IdentityCredential, IdentityPhone, IdentityProfile
from pincer.models.onboarding import Onboarding
from pincer.models.sessions import ChatSession, SessionImage
from pincer.repositories.sessions import SessionImageRepository, SessionRepository
from pincer.services.identity import IdentityService
from pincer.services.sessions import ImageSet, SessionService, StoredImage

_TABLES = [
    IdentityProfile.__table__,
    ChannelIdentity.__table__,
    IdentityCredential.__table__,
    IdentityPhone.__table__,
    AuthSession.__table__,
    ChatSession.__table__,
    SessionImage.__table__,
    Onboarding.__table__,
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


def photo(n: int) -> ImageContent:
    return ImageContent.from_bytes(f"photo-{n}".encode() * 8, "image/jpeg")


def manager_on(url: str, *, max_messages: int = 50) -> SessionManager:
    # Attached to `url` directly: `initialize()` only takes a SQLite path, so this runs on either dialect.
    manager = SessionManager(Path(), max_messages=max_messages)
    manager._service = SessionService(url)
    return manager


async def say(manager: SessionManager, session: Session, text: str, *photos: ImageContent) -> None:
    await manager.add_message(session, LLMMessage(role=MessageRole.USER, content=text, images=list(photos)))


async def photo_rows(url: str) -> dict[str, set[str]]:
    """session id -> the hashes of the photos stored for it."""
    async with session_scope(url) as session:
        rows = await SessionImageRepository(session).list()
    found: dict[str, set[str]] = {}
    for row in rows:
        found.setdefault(row.session_id, set()).add(row.sha256)
    return found


async def stored_refs(url: str, session_id: str) -> list[list[str]]:
    row = await SessionService(url).load(session_id, user_id="", channel="", legacy_fallback=False)
    assert row is not None
    return [[ref["sha256"] for ref in m.get("images", [])] for m in json.loads(row["messages"])]


# ── storing ──────────────────────────────────────────────────────────


async def test_the_same_photo_sent_twice_in_a_session_is_stored_once(url):
    manager = manager_on(url)
    session = await manager.get_or_create("alice", "telegram")
    same, other = photo(1), photo(2)

    await say(manager, session, "look", same)
    await say(manager, session, "look again", same)  # a later save
    session.messages.append(LLMMessage(role=MessageRole.USER, content="and once more", images=[same, other]))
    await manager.save(session)  # two more refs in one save

    assert await stored_refs(url, "telegram:alice") == [[same.sha256], [same.sha256], [same.sha256, other.sha256]]
    assert await photo_rows(url) == {"telegram:alice": {same.sha256, other.sha256}}


async def test_a_stored_photo_is_never_overwritten(url):
    service = SessionService(url)
    await service.save(
        session_id="telegram:alice",
        user_id="alice",
        channel="telegram",
        messages="[]",
        metadata="{}",
        created_at=1.0,
        updated_at=1.0,
    )
    first = StoredImage("a" * 64, "image/jpeg", b"first bytes")
    for data in (b"first bytes", b"other bytes"):
        await service.save(
            session_id="telegram:alice",
            user_id="alice",
            channel="telegram",
            messages="[]",
            metadata="{}",
            created_at=1.0,
            updated_at=1.0,
            images=ImageSet(frozenset({first.sha256}), (StoredImage(first.sha256, "image/jpeg", data),)),
        )

    row = await service.load("telegram:alice", user_id="alice", channel="telegram", legacy_fallback=False)
    assert row is not None
    assert [(i.sha256, i.data) for i in row["images"]] == [(first.sha256, b"first bytes")]


# ── pruning: photos follow their messages out ────────────────────────


async def test_clearing_a_session_deletes_its_photos_and_only_its_own(url):
    manager = manager_on(url)
    mine = await manager.get_or_create("alice", "telegram")
    theirs = await manager.get_or_create("bob", "telegram")
    await say(manager, mine, "one", photo(1), photo(2))
    await say(manager, theirs, "mine", photo(1))  # the same photo, in another session
    assert await photo_rows(url) == {  # precondition: both sessions hold photos
        "telegram:alice": {photo(1).sha256, photo(2).sha256},
        "telegram:bob": {photo(1).sha256},
    }

    await manager.clear(mine)

    assert await photo_rows(url) == {"telegram:bob": {photo(1).sha256}}


async def test_clearing_deletes_photos_this_process_never_saw(url):
    """A row an earlier process stored (or a crash left) is not in the session's own record of its photos."""
    manager = manager_on(url)
    session = await manager.get_or_create("alice", "telegram")
    await say(manager, session, "one", photo(1))
    session._synced_images = frozenset()  # as if another process had stored the photo
    assert await photo_rows(url) == {"telegram:alice": {photo(1).sha256}}  # precondition

    await manager.clear(session)

    assert await photo_rows(url) == {}


async def test_a_repair_that_removes_a_message_deletes_only_the_photos_nothing_holds_any_more(url):
    """`Agent._repair_tool_pairs` and its clear fallback replace `session.messages` and save."""
    manager = manager_on(url)
    session = await manager.get_or_create("alice", "telegram")
    await say(manager, session, "kept", photo(1))
    await say(manager, session, "orphaned", photo(2))
    assert await photo_rows(url) == {"telegram:alice": {photo(1).sha256, photo(2).sha256}}  # precondition

    session.messages = session.messages[:1]
    await manager.save(session)
    assert await photo_rows(url) == {"telegram:alice": {photo(1).sha256}}

    session.messages = [m for m in session.messages if m.role == MessageRole.SYSTEM]  # the clear fallback
    await manager.save(session)
    assert await photo_rows(url) == {}


async def test_the_client_summary_swap_deletes_the_photos_of_the_messages_it_replaced(url):
    manager = manager_on(url)
    session = await manager.get_or_create("alice", "telegram")
    await say(manager, session, "old", photo(1))
    await say(manager, session, "newer", photo(2))
    await say(manager, session, "newest", photo(3), photo(1))  # also shows the old photo again
    prefix = session.messages[:2]
    summary = LLMMessage(role=MessageRole.SYSTEM, content="summary")
    assert len(await photo_rows(url)) == 1  # precondition: three photos stored

    assert await manager.compact(session, prefix, summary)

    # photo 2 left with its message; photo 1 stays: a kept message still shows it
    assert await photo_rows(url) == {"telegram:alice": {photo(3).sha256, photo(1).sha256}}


async def test_trimming_a_session_deletes_the_photos_of_the_messages_it_cut(url):
    manager = manager_on(url, max_messages=10)
    session = await manager.get_or_create("alice", "telegram")
    async with manager.turn("alice", "telegram") as inside:
        assert inside is session
        for n in range(1, 13):  # past the cap of 10; nothing is cut while the turn runs
            await say(manager, session, f"message {n}", photo(n))
        assert len(await photo_rows(url)) == 1
        assert len(next(iter((await photo_rows(url)).values()))) == 12  # precondition: every photo stored

    # cut to 3/5 of the cap: the newest six messages stay
    assert [m.content for m in session.messages] == [f"message {n}" for n in range(7, 13)]
    assert await photo_rows(url) == {"telegram:alice": {photo(n).sha256 for n in range(7, 13)}}


async def test_trimming_between_turns_deletes_the_photos_of_the_messages_it_cut(url):
    manager = manager_on(url, max_messages=10)
    session = await manager.get_or_create("alice", "telegram")
    for n in range(1, 12):  # the 11th message passes the cap of 10
        await say(manager, session, f"message {n}", photo(n))

    assert [m.content for m in session.messages] == [f"message {n}" for n in range(6, 12)]
    assert await photo_rows(url) == {"telegram:alice": {photo(n).sha256 for n in range(6, 12)}}


# ── identity merge ───────────────────────────────────────────────────


async def store(url: str, session_id: str, *, user_id: str, at: float, channel: str = "whatsapp", photos=()) -> None:
    """A session as the main message path leaves it: one user message showing `photos`."""
    refs = [p.to_ref() for p in photos]
    await SessionService(url).save(
        session_id=session_id,
        user_id=user_id,
        channel=channel,
        messages=json.dumps([{"role": "user", "content": f"from {user_id}", "images": refs}]),
        metadata="{}",
        created_at=at,
        updated_at=at,
        images=ImageSet(
            frozenset(p.sha256 for p in photos),
            tuple(StoredImage(p.sha256, p.media_type, base64.b64decode(p.data)) for p in photos),
        ),
    )


async def merge_setup(url: str) -> IdentityService:
    service = IdentityService(url)
    await service.create_identity("usr_hash", channel="telegram", channel_user_id="12345")
    await service.create_identity("alice", channel="whatsapp", channel_user_id="4930111")
    await store(url, "whatsapp:carol", user_id="carol", at=1.0, photos=[photo(9)])  # nothing to do with the merge
    return service


async def test_a_merge_moves_the_photos_with_the_session(url):
    service = await merge_setup(url)
    await store(url, "whatsapp:usr_hash", user_id="usr_hash", at=1.0, photos=[photo(1), photo(2)])
    assert (await photo_rows(url))["whatsapp:usr_hash"] == {photo(1).sha256, photo(2).sha256}  # precondition

    await service.rename_identity("usr_hash", "alice")

    assert await photo_rows(url) == {
        "whatsapp:alice": {photo(1).sha256, photo(2).sha256},
        "whatsapp:carol": {photo(9).sha256},
    }
    reloaded = await manager_on(url).get_or_create("alice", "whatsapp")
    assert [i.sha256 for i in reloaded.messages[0].images] == [photo(1).sha256, photo(2).sha256]


@pytest.mark.parametrize(
    ("old_at", "target_at", "winner"), [(2.0, 1.0, "old"), (1.0, 2.0, "target"), (1.0, 1.0, "target")]
)
async def test_a_merge_collision_keeps_the_photos_of_the_session_it_keeps(url, old_at, target_at, winner):
    service = await merge_setup(url)
    await store(url, "whatsapp:usr_hash", user_id="usr_hash", at=old_at, photos=[photo(1)])
    await store(url, "whatsapp:alice", user_id="alice", at=target_at, photos=[photo(2)])
    assert set(await photo_rows(url)) == {"whatsapp:usr_hash", "whatsapp:alice", "whatsapp:carol"}  # precondition

    await service.rename_identity("usr_hash", "alice")

    kept = photo(1) if winner == "old" else photo(2)
    assert await photo_rows(url) == {"whatsapp:alice": {kept.sha256}, "whatsapp:carol": {photo(9).sha256}}
    assert await stored_refs(url, "whatsapp:alice") == [[kept.sha256]]


# ── the foreign key ──────────────────────────────────────────────────


async def test_the_photo_table_hangs_off_its_session_with_both_foreign_key_actions_cascading(url):
    async with get_engine(url).connect() as conn:
        foreign_keys = await conn.run_sync(lambda c: sa.inspect(c).get_foreign_keys("pincer_session_images"))
        primary_key = await conn.run_sync(lambda c: sa.inspect(c).get_pk_constraint("pincer_session_images"))
        columns = await conn.run_sync(lambda c: sa.inspect(c).get_columns("pincer_session_images"))
    assert [
        (fk["constrained_columns"], fk["referred_table"], fk["referred_columns"], fk["options"].get("ondelete"))
        for fk in foreign_keys
    ] == [(["session_id"], "pincer_sessions", ["session_id"], "CASCADE")]
    assert [fk["options"].get("onupdate") for fk in foreign_keys] == ["CASCADE"]
    assert primary_key["constrained_columns"] == ["session_id", "sha256"]
    assert {c["name"]: c["nullable"] for c in columns} == {
        "session_id": False,
        "sha256": False,
        "media_type": False,
        "data": False,
        "created_at": False,
    }


async def test_deleting_a_session_row_deletes_its_photos(url):
    """Postgres cascades. The app's SQLite connections run with foreign keys off (`db.engine`), so
    there the cascade is checked on a connection that turns them on: the DDL carries it."""
    await store(url, "whatsapp:alice", user_id="alice", at=1.0, photos=[photo(1)])
    await store(url, "whatsapp:bob", user_id="bob", at=1.0, photos=[photo(2)])
    assert set(await photo_rows(url)) == {"whatsapp:alice", "whatsapp:bob"}  # precondition

    engine = get_engine(url)
    if engine.dialect.name == "sqlite":
        database = engine.url.database
        assert database is not None
        raw = sqlite3.connect(database)
        try:
            raw.execute("PRAGMA foreign_keys=ON")
            assert raw.execute("PRAGMA foreign_keys").fetchone() == (1,)  # precondition: it is enforced here
            raw.execute("DELETE FROM pincer_sessions WHERE session_id = 'whatsapp:alice'")
            raw.commit()
        finally:
            raw.close()
    else:
        async with session_scope(url) as session:
            await SessionRepository(session).delete_where(col(ChatSession.session_id) == "whatsapp:alice")

    assert await photo_rows(url) == {"whatsapp:bob": {photo(2).sha256}}


async def test_only_the_session_repository_deletes_session_rows():
    """Every path that deletes a `pincer_sessions` row must delete its photos too (SQLite does not cascade).

    Today that is `SessionRepository.rewrite_session_ids`; `SessionManager.clear` keeps the row. A new
    module touching the table fails here until its author has dealt with the photos.
    """
    source = Path(__file__).resolve().parents[2] / "src" / "pincer"
    allowed = {
        "models/sessions.py",
        "models/onboarding.py",  # only names the table in a docstring
        "repositories/sessions.py",
    }
    touching = {
        path.relative_to(source).as_posix()
        for path in source.rglob("*.py")
        if "db/migrations" not in path.as_posix()
        and ("ChatSession" in path.read_text() or "pincer_sessions" in path.read_text())
    }
    assert touching == allowed


# ── loading ──────────────────────────────────────────────────────────


async def test_a_ref_without_a_stored_photo_loads_without_it_and_is_logged(url, caplog):
    kept, lost = photo(1), photo(2)
    await store(url, "whatsapp:alice", user_id="alice", at=1.0, photos=[kept])
    row = await SessionService(url).load("whatsapp:alice", user_id="alice", channel="whatsapp", legacy_fallback=False)
    assert row is not None
    messages = json.loads(row["messages"])
    messages[0]["images"].append(lost.to_ref())  # a ref whose row is gone
    await SessionService(url).save(
        session_id="whatsapp:alice",
        user_id="alice",
        channel="whatsapp",
        messages=json.dumps(messages),
        metadata="{}",
        created_at=1.0,
        updated_at=1.0,
    )

    with caplog.at_level(logging.WARNING, logger="pincer.core.session"):
        session = await manager_on(url).get_or_create("alice", "whatsapp")

    assert [i.sha256 for i in session.messages[0].images] == [kept.sha256]  # the one it has is still there
    warnings = [r.getMessage() for r in caplog.records if r.name == "pincer.core.session"]
    assert warnings == ["Session whatsapp:alice refers to 1 photo(s) its image store lacks; left out"]


async def test_a_session_saved_before_photos_were_kept_loads_as_it_was(url):
    old = '[{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]'
    await SessionService(url).save(
        session_id="whatsapp:alice",
        user_id="alice",
        channel="whatsapp",
        messages=old,
        metadata="{}",
        created_at=1.0,
        updated_at=1.0,
    )

    session = await manager_on(url).get_or_create("alice", "whatsapp")

    assert [(m.role, m.content, m.images) for m in session.messages] == [
        (MessageRole.USER, "hello", []),
        (MessageRole.ASSISTANT, "hi", []),
    ]


async def test_a_session_loaded_from_its_pre_unified_key_stores_its_photos_under_the_new_key(url):
    """Before cross-channel identities a session was keyed `channel:user_id`; it is read from there
    (photos too) and saved under the unified key, with its photos."""
    shown = photo(1)
    await store(url, "telegram:alice", user_id="alice", at=1.0, channel="telegram", photos=[shown])
    manager = manager_on(url)
    session = await manager.get_or_create("alice", "telegram", pincer_user_id="usr_a")
    assert session.session_id == "unified:usr_a"
    assert [i.sha256 for i in session.messages[0].images] == [shown.sha256]  # precondition: read from the old key

    await say(manager, session, "and now?")

    assert await photo_rows(url) == {"telegram:alice": {shown.sha256}, "unified:usr_a": {shown.sha256}}
    again = await manager_on(url).get_or_create("alice", "telegram", pincer_user_id="usr_a")
    assert [i.sha256 for i in again.messages[0].images] == [shown.sha256]
