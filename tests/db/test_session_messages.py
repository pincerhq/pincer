"""Session messages as rows (`pincer_session_messages`, issue #406), on both dialects.

What the move from one JSON list to a row per message must keep: the history's order, every message's
provider output (`raw`) and tool calls exactly as written (they are sent to the LLM again, and the
latest replayable output and compaction window are found by position), and saves that write only what
changed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from pincer.core.agent import _sanitize_tool_pairs
from pincer.core.session import Session, SessionManager
from pincer.db.engine import get_engine
from pincer.db.ids import new_id, uuid7_after, uuid7_at, uuid7_ms
from pincer.db.session import session_scope
from pincer.db.types import epoch_ms, from_epoch_ms, from_epoch_seconds, utc_text
from pincer.llm.base import LLMMessage, MessageRole, ToolCall
from pincer.llm.openai_responses import last_window_index
from pincer.llm.replay import replayable_raw
from pincer.models.identity import IdentityProfile
from pincer.models.sessions import ChatSession, SessionImage, SessionMessage
from pincer.repositories.sessions import SessionMessageRepository, SessionRepository
from pincer.services.sessions import MessageChanges, SessionService

_TABLES = [IdentityProfile.__table__, ChatSession.__table__, SessionMessage.__table__, SessionImage.__table__]
_WIRE = "anthropic:claude-x"
_OPENAI = "openai:gpt-x"


@pytest.fixture
async def url(db_url: str):
    engine = get_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: IdentityProfile.metadata.drop_all(c, tables=_TABLES))
        await conn.run_sync(lambda c: IdentityProfile.metadata.create_all(c, tables=_TABLES))
    yield db_url
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: IdentityProfile.metadata.drop_all(c, tables=_TABLES))


def manager_on(url: str) -> SessionManager:
    manager = SessionManager(Path())
    manager._service = SessionService(url)
    return manager


async def reload(url: str, user_id: str = "alice", channel: str = "web") -> Session:
    """The session as a fresh process reads it."""
    return await manager_on(url).get_or_create(user_id, channel)


def spy_saves(manager: SessionManager) -> list[MessageChanges]:
    saves: list[MessageChanges] = []
    real = manager._store.save

    async def spy(**kwargs: Any) -> None:
        saves.append(kwargs["messages"])
        await real(**kwargs)

    manager._store.save = spy  # type: ignore[method-assign]
    return saves


def thinking_reply(n: int, *, tool: ToolCall | None = None) -> LLMMessage:
    """An assistant message as a provider's response leaves it: text, maybe a call, and the raw output."""
    raw: list[dict[str, Any]] = [{"type": "thinking", "thinking": f"thought {n}", "signature": f"SIG{n}"}]
    if tool is not None:
        raw.append({"type": "tool_use", "id": tool.id, "name": tool.name, "input": tool.arguments})
    else:
        raw.append({"type": "text", "text": f"answer {n}"})
    return LLMMessage(
        role=MessageRole.ASSISTANT,
        content="" if tool else f"answer {n}",
        tool_calls=[tool] if tool else [],
        raw=raw,
        wire=_WIRE,
    )


def window(n: int) -> LLMMessage:
    """An OpenAI compaction window: a system message whose raw output is the encrypted compaction item."""
    return LLMMessage(
        role=MessageRole.SYSTEM,
        content="[compacted]",
        raw=[{"type": "compaction", "id": f"cmp_{n}", "encrypted_content": f"ENC{n}"}],
        wire=_OPENAI,
    )


def user(text: str) -> LLMMessage:
    return LLMMessage(role=MessageRole.USER, content=text)


async def rows_of(url: str, session_id: str) -> list[SessionMessage]:
    async with session_scope(url) as session:
        row = await SessionRepository(session).get_by(session_id=session_id)
        assert row is not None
        return list(await SessionMessageRepository(session).for_session(row.id))


# ── ids and times ────────────────────────────────────────────────────


def test_an_id_after_another_in_the_same_millisecond_sorts_after_it_and_keeps_the_millisecond():
    first = uuid7_at(1_000, 7)
    after = uuid7_after(1_000, first)
    assert (uuid7_ms(after), after > first) == (1_000, True)
    assert uuid7_ms(uuid7_after(2_000, first)) == 2_000  # a later millisecond starts over
    earlier = uuid7_after(500, first)  # cannot carry 500 and sort after: takes the previous millisecond
    assert (uuid7_ms(earlier), earlier > first) == (1_000, True)
    assert uuid7_ms(uuid7_after(1_000, None)) == 1_000


def test_epoch_milliseconds_are_cut_not_rounded_and_survive_float_noise():
    assert epoch_ms(datetime(1970, 1, 1, 0, 0, 1, 999_999, tzinfo=UTC)) == 1_999
    assert from_epoch_seconds(100.123) == from_epoch_ms(100_123)  # 100.123 is 100.12299999... as a float
    assert utc_text(from_epoch_ms(100_123)) == "1970-01-01T00:01:40.123+00:00"


async def test_a_message_time_keeps_its_milliseconds_and_nothing_finer_on_both_dialects(url):
    at = datetime(2026, 11, 21, 9, 30, 15, 123_987, tzinfo=timedelta_zone(2))
    await save(url, [{"role": "user", "content": "hi", "id": new_id(), "created_at": at}])

    (row,) = await rows_of(url, "web:alice")
    assert row.created_at == datetime(2026, 11, 21, 7, 30, 15, 123_000, tzinfo=UTC)  # UTC, cut to the millisecond
    assert row.created_at.tzinfo == UTC


async def test_sqlite_stores_the_time_as_fixed_width_utc_text(url):
    if not url.startswith("sqlite"):
        pytest.skip("Postgres stores a timestamptz")
    for at in (from_epoch_ms(5), from_epoch_ms(1_788_256_800_123)):
        await save(url, [{"role": "user", "content": "hi", "id": new_id(), "created_at": at}])
    async with get_engine(url).connect() as conn:
        texts = (await conn.execute(sa.text("SELECT created_at FROM pincer_session_messages"))).scalars().all()
    assert sorted(texts) == ["1970-01-01T00:00:00.005+00:00", "2026-09-01T10:00:00.123+00:00"]
    assert {len(t) for t in texts} == {29}


async def test_a_naive_time_is_refused(url):
    with pytest.raises(sa.exc.StatementError, match="naive"):
        await save(url, [{"role": "user", "content": "hi", "id": new_id(), "created_at": datetime(2026, 1, 1)}])


def timedelta_zone(hours: int):
    from datetime import timezone

    return timezone(timedelta(hours=hours))


async def save(url: str, inserts: list[dict[str, Any]], *, session_id: str = "web:alice") -> None:
    at = from_epoch_ms(1_000)
    await SessionService(url).save(
        session_id=session_id,
        user_id="alice",
        channel="web",
        metadata="{}",
        created_at=at,
        updated_at=at,
        messages=MessageChanges(insert=inserts),
    )


# ── what a row holds ─────────────────────────────────────────────────


async def test_raw_output_and_tool_calls_come_back_with_their_keys_in_the_order_written(url):
    """`json`, not `jsonb`, on Postgres: a request rebuilt after a reload must be the request sent before
    it, or a prompt cache keyed on that prefix misses (and `jsonb` sorts keys)."""
    call = ToolCall(id="t1", name="lookup", arguments={"zone": "eu", "after": 2, "b": {"y": 1, "x": 2}})
    manager = manager_on(url)
    session = await manager.get_or_create("alice", "web")
    await manager.add_message(session, user("look it up"))
    await manager.add_message(session, thinking_reply(1, tool=call))
    before = [json.dumps(m.to_dict()) for m in session.messages]

    again = await reload(url)

    assert [json.dumps(m.to_dict()) for m in again.messages] == before


async def test_a_nul_character_is_stored_as_a_replacement_character_in_text_and_kept_in_json(url):
    manager = manager_on(url)
    session = await manager.get_or_create("alice", "web")
    await manager.add_message(session, user("a\x00b"))
    reply = thinking_reply(1)
    reply.raw[1]["text"] = "c\x00d"
    await manager.add_message(session, reply)

    again = await reload(url)

    assert again.messages[0].content == "a�b"
    assert again.messages[1].raw[1]["text"] == "c\x00d"  # escaped as \u0000 in the JSON, which `json` keeps


@pytest.mark.parametrize(
    ("role", "column", "value"),
    [
        ("user", "tool_calls", [{"id": "t", "name": "n", "arguments": {}}]),
        ("assistant", "tool_call_id", "t1"),
        ("tool_result", "raw", [{"type": "text", "text": "x"}]),
        ("assistant", "updated_at", from_epoch_ms(1)),
        ("system", "channel_message_id", "m1"),
        ("bot", None, None),
    ],
)
async def test_the_table_refuses_a_field_the_role_cannot_have(url, role, column, value):
    await save(url, [])
    async with session_scope(url) as session:
        ref = (await SessionRepository(session).get_by(session_id="web:alice")).id
    row = {"id": new_id(), "session_id": ref, "role": role, "content": "", "created_at": from_epoch_ms(2)}
    if column:
        row[column] = value
    with pytest.raises(IntegrityError):
        async with session_scope(url) as session:
            session.add(SessionMessage(**row))


async def test_the_service_drops_a_field_the_role_cannot_have_instead_of_failing_the_save(url, caplog):
    await save(
        url, [{"role": "user", "content": "hi", "tool_call_id": "t1", "id": new_id(), "created_at": from_epoch_ms(1)}]
    )

    (row,) = await rows_of(url, "web:alice")
    assert row.tool_call_id is None
    assert "Not storing tool_call_id on a user message" in caplog.text


# ── order, and the latest raw output ─────────────────────────────────


async def test_the_history_comes_back_in_order_with_messages_saved_in_one_millisecond(url):
    manager = manager_on(url)
    session = await manager.get_or_create("alice", "web")
    calls = [ToolCall(id=f"t{n}", name="lookup", arguments={"n": n}) for n in range(5)]
    await manager.add_message(session, user("go"))
    await manager.add_message(session, LLMMessage(role=MessageRole.ASSISTANT, content="", tool_calls=calls))
    results = [LLMMessage(role=MessageRole.TOOL_RESULT, content=f"r{n}", tool_call_id=f"t{n}") for n in range(5)]
    await manager.add_messages(session, results)  # one save, one instant

    again = await reload(url)

    assert [m.to_dict() for m in again.messages] == [m.to_dict() for m in session.messages]
    times = [m.created_at for m in again.messages]
    assert times == sorted(times)


async def test_the_latest_raw_output_and_window_are_still_the_latest_after_a_reload(url):
    """Every request replays each assistant message's raw output while its wire matches, and only the
    last compaction window; both are found by position. Rows ordered by time keep those positions."""
    manager = manager_on(url)
    session = await manager.get_or_create("alice", "web")
    for message in (
        window(1),
        user("one"),
        thinking_reply(1),
        window(2),
        user("two"),
        thinking_reply(2),
        user("three"),
    ):
        await manager.add_message(session, message)

    again = await reload(url)

    assert last_window_index(again.messages, _OPENAI) == last_window_index(session.messages, _OPENAI) == 3
    replayed = [replayable_raw(m, _WIRE) for m in again.messages if m.role is MessageRole.ASSISTANT]
    assert replayed == [replayable_raw(m, _WIRE) for m in session.messages if m.role is MessageRole.ASSISTANT]
    latest = next(m for m in reversed(again.messages) if m.raw and m.role is MessageRole.ASSISTANT)
    assert latest.raw[0]["signature"] == "SIG2"


async def test_a_reloaded_history_appends_after_its_last_message(url):
    manager = manager_on(url)
    session = await manager.get_or_create("alice", "web")
    await manager.add_message(session, user("one"))
    await manager.add_message(session, thinking_reply(1))

    later = manager_on(url)
    again = await later.get_or_create("alice", "web")
    await later.add_message(again, user("two"))
    await later.add_message(again, thinking_reply(2))

    final = await reload(url)
    assert [m.content for m in final.messages] == ["one", "answer 1", "two", "answer 2"]


# ── saves write only what changed ────────────────────────────────────


async def test_a_turn_inserts_only_its_new_messages(url):
    manager = manager_on(url)
    session = await manager.get_or_create("alice", "web")
    await manager.add_message(session, user("one"))
    saves = spy_saves(manager)

    await manager.add_message(session, thinking_reply(1))
    await manager.add_messages(session, [user("two"), thinking_reply(2)])
    await manager.save(session)  # nothing changed in between

    assert [(len(s.insert), len(s.update), len(s.delete)) for s in saves] == [(1, 0, 0), (2, 0, 0), (0, 0, 0)]


async def test_a_save_retried_after_one_that_landed_but_failed_adds_nothing_twice(url):
    """The JSON list was overwritten whole, so a retried save was harmless. Rows are inserted: a save
    whose commit went through but whose caller saw an error (a dropped connection) is retried with the
    same rows, which overwrite themselves."""
    manager = manager_on(url)
    session = await manager.get_or_create("alice", "web")
    await manager.add_message(session, user("one"))
    real = manager._store.save
    failures: list[None] = []

    async def lands_then_fails(**kwargs: Any) -> None:
        await real(**kwargs)
        if not failures:
            failures.append(None)
            raise OSError("connection lost after the commit")

    manager._store.save = lands_then_fails  # type: ignore[method-assign]
    with pytest.raises(OSError):
        await manager.add_messages(session, [thinking_reply(1), user("two")])
    await manager.save(session)

    assert [r.content for r in await rows_of(url, "web:alice")] == ["one", "answer 1", "two"]
    assert [m.content for m in (await reload(url)).messages] == ["one", "answer 1", "two"]


async def test_an_unchanged_session_is_not_written_when_evicted(url):
    manager = manager_on(url)
    session = await manager.get_or_create("alice", "web")
    await manager.add_message(session, user("one"))
    saves = spy_saves(manager)

    await manager.evict("web:alice")

    assert saves == []


async def test_a_pair_repair_rewrites_the_message_it_rebuilt_in_place(url):
    """The repaired message keeps its row and time: it stays where it was, not after the reply."""
    manager = manager_on(url)
    session = await manager.get_or_create("alice", "web")
    calls = [ToolCall(id="ta", name="a", arguments={}), ToolCall(id="tb", name="b", arguments={})]
    await manager.add_message(session, user("go"))
    await manager.add_message(session, LLMMessage(role=MessageRole.ASSISTANT, content="calling", tool_calls=calls))
    await manager.add_message(session, LLMMessage(role=MessageRole.TOOL_RESULT, content="a done", tool_call_id="ta"))
    await manager.add_message(session, user("and?"))
    rebuilt_row = session.messages[1].row_id
    saves = spy_saves(manager)

    session.messages = _sanitize_tool_pairs(session.messages)  # `tb` was never answered
    await manager.save(session)

    ((changes),) = saves
    assert ([r["id"] for r in changes.update], changes.insert, changes.delete) == ([rebuilt_row], [], [])
    again = await reload(url)
    assert [(m.role, m.content) for m in again.messages] == [
        (MessageRole.USER, "go"),
        (MessageRole.ASSISTANT, "calling"),
        (MessageRole.TOOL_RESULT, "a done"),
        (MessageRole.USER, "and?"),
    ]
    assert [c.id for c in again.messages[1].tool_calls] == ["ta"]


async def test_a_compaction_summary_takes_the_row_of_the_first_message_it_replaces(url):
    manager = manager_on(url)
    session = await manager.get_or_create("alice", "web")
    for message in (user("one"), thinking_reply(1), user("two"), thinking_reply(2), user("three")):
        await manager.add_message(session, message)
    prefix = session.messages[:4]
    first_row, first_at = prefix[0].row_id, prefix[0].created_at
    saves = spy_saves(manager)

    summary = LLMMessage(role=MessageRole.SYSTEM, content="[Previous conversation summary]\nearlier")
    assert await manager.compact(session, prefix, summary)

    ((changes),) = saves
    assert [r["id"] for r in changes.update] == [first_row]
    assert (changes.insert, sorted(changes.delete)) == ([], sorted(m.row_id for m in prefix[1:]))
    again = await reload(url)
    assert [(m.role, m.content) for m in again.messages] == [
        (MessageRole.SYSTEM, summary.content),
        (MessageRole.USER, "three"),
    ]
    assert again.messages[0].created_at == first_at


async def test_a_message_put_before_stored_ones_moves_them_after_it(url):
    """No code path does this today; if one did, the order would still be the list's."""
    manager = manager_on(url)
    session = await manager.get_or_create("alice", "web")
    await manager.add_message(session, user("one"))
    await manager.add_message(session, user("three"))

    session.messages.insert(1, user("two"))
    await manager.save(session)

    again = await reload(url)
    assert [m.content for m in again.messages] == ["one", "two", "three"]


async def test_clearing_deletes_every_message_row(url):
    manager = manager_on(url)
    session = await manager.get_or_create("alice", "web")
    await manager.add_message(session, user("one"))
    await manager.add_message(session, thinking_reply(1))

    await manager.clear(session)

    assert await rows_of(url, "web:alice") == []


async def test_a_session_read_from_its_old_key_stores_its_messages_anew_at_their_own_times(url):
    old = manager_on(url)
    legacy = await old.get_or_create("alice", "telegram")
    await old.add_message(legacy, user("before identities"))
    said_at = legacy.messages[0].created_at

    manager = manager_on(url)
    session = await manager.get_or_create("alice", "telegram", pincer_user_id="usr_a")
    assert session.session_id == "unified:usr_a"
    await manager.add_message(session, user("after"))

    unified = await rows_of(url, "unified:usr_a")
    assert [(r.content, r.created_at == said_at) for r in unified] == [("before identities", True), ("after", False)]
    assert [r.content for r in await rows_of(url, "telegram:alice")] == ["before identities"]  # left as it was


# ── what people see, and edits ───────────────────────────────────────


async def test_a_history_page_leaves_out_context_and_raw_and_pages_back_by_time(url):
    manager = manager_on(url)
    session = await manager.get_or_create("alice", "web")
    for n in range(5):
        message = user(f"u{n}")
        message.context = "[memories]"
        await manager.add_message(session, message)
    service = SessionService(url)

    newest = await service.history("web:alice", limit=2)
    older = await service.history("web:alice", limit=2, before=(newest[0]["created_at"], newest[0]["id"]))

    assert [m["content"] for m in newest] == ["u3", "u4"]
    assert [m["content"] for m in older] == ["u1", "u2"]
    assert set(newest[0]) == {"id", "role", "content", "tool_calls", "images", "created_at", "updated_at"}
    assert await service.history("web:nobody", limit=2) == []


async def test_an_edit_rewrites_the_user_message_the_messenger_names_and_stamps_it(url):
    ref_message = {"role": "user", "content": "helo", "id": new_id(), "created_at": from_epoch_ms(1)}
    await save(url, [ref_message])
    edited_at = from_epoch_ms(9_000)
    async with session_scope(url) as session:
        ref = (await SessionRepository(session).get_by(session_id="web:alice")).id
        await session.exec(
            sa.update(SessionMessage).where(SessionMessage.id == ref_message["id"]).values(channel_message_id="tg-42")
        )
        repo = SessionMessageRepository(session)
        assert await repo.edit(ref, "tg-42", "hello", at=edited_at) is True
        assert await repo.edit(ref, "tg-43", "nope", at=edited_at) is False

    (row,) = await rows_of(url, "web:alice")
    assert (row.content, row.updated_at, row.created_at) == ("hello", edited_at, from_epoch_ms(1))
