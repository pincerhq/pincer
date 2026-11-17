"""SessionManager and the session key format."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

import pytest

from pincer.core.session import ONBOARDING_PROMPT_SENT_KEY, SessionManager, conversation_key
from pincer.llm.base import ImageContent, LLMMessage, MessageRole
from pincer.services.sessions import SessionService

if TYPE_CHECKING:
    from pathlib import Path

CONVERSATION = "22222222-2222-4222-8222-222222222222"


def test_conversation_key_nests_under_the_channel_key():
    assert conversation_key("alice", "web", "2222aaaa-2222-4222-8222-222222222222") == (
        "web:alice:2222aaaa-2222-4222-8222-222222222222"
    )


async def test_main_session_starts_fresh_when_only_conversations_exist(session_manager: SessionManager):
    """A user whose only web traffic was a conversation gets an empty main
    session, not that conversation's history."""
    key = conversation_key("u", "web", CONVERSATION)
    conversation = await session_manager.get_or_create("u", "web", session_key=key)
    await session_manager.add_message(conversation, LLMMessage(role=MessageRole.USER, content="conversation only"))
    await session_manager.evict(key)

    main = await session_manager.get_or_create("u", "web")

    assert main.session_id == "web:u"
    assert [m.content for m in main.messages] == []


@pytest.fixture
async def capped(tmp_path: Path):
    """A manager that caches at most two sessions."""
    sm = SessionManager(tmp_path / "test.db", max_cached=2)
    await sm.initialize()
    yield sm
    await sm.close()


async def test_the_cache_drops_the_least_recently_used_session_past_its_cap(capped: SessionManager):
    for user in ("a", "b", "c"):
        await capped.get_or_create(user, "web")

    assert list(capped._cache) == ["web:b", "web:c"]


async def test_a_recently_used_session_survives(capped: SessionManager):
    await capped.get_or_create("a", "web")
    await capped.get_or_create("b", "web")
    await capped.get_or_create("a", "web")  # a hit makes a the most recent

    await capped.get_or_create("c", "web")

    assert list(capped._cache) == ["web:a", "web:c"]


async def test_an_evicted_session_reloads_with_its_messages_and_metadata(capped: SessionManager):
    a = await capped.get_or_create("a", "web")
    await capped.add_message(a, LLMMessage(role=MessageRole.USER, content="remember me"))
    a.metadata[ONBOARDING_PROMPT_SENT_KEY] = "true"  # in memory only: no add_message after it
    await capped.get_or_create("b", "web")
    await capped.get_or_create("c", "web")
    assert "web:a" not in capped._cache

    reloaded = await capped.get_or_create("a", "web")

    assert reloaded is not a
    assert [m.content for m in reloaded.messages] == ["remember me"]
    assert reloaded.metadata.get(ONBOARDING_PROMPT_SENT_KEY) == "true"


async def test_close_persists_every_cached_session(tmp_path: Path):
    sm = SessionManager(tmp_path / "test.db", max_cached=2)
    await sm.initialize()
    for user in ("a", "b"):
        (await sm.get_or_create(user, "web")).metadata[ONBOARDING_PROMPT_SENT_KEY] = "true"
    await sm.close()

    again = SessionManager(tmp_path / "test.db", max_cached=2)
    await again.initialize()
    try:
        for user in ("a", "b"):
            reloaded = await again.get_or_create(user, "web")
            assert reloaded.metadata.get(ONBOARDING_PROMPT_SENT_KEY) == "true", user
    finally:
        await again.close()


def _on_save(sm: SessionManager, hook):
    """Run `hook(session_id)` inside the store's save, before the write,
    to stand in for a request that lands while a save is awaited."""
    store = sm._store
    real_save = store.save

    async def save(**values):
        await hook(values["session_id"])
        await real_save(**values)

    store.save = save  # type: ignore[method-assign]


async def test_close_survives_a_cache_hit_during_its_save(tmp_path: Path):
    sm = SessionManager(tmp_path / "test.db")
    await sm.initialize()
    for user in ("a", "b"):
        (await sm.get_or_create(user, "web")).metadata[ONBOARDING_PROMPT_SENT_KEY] = "true"
    hit = []

    async def hit_it_again(session_id: str) -> None:
        # The first save is web:a's; a hit moves it behind web:b.
        if not hit:
            hit.append(session_id)
            await sm.get_or_create(session_id.removeprefix("web:"), "web")

    _on_save(sm, hit_it_again)
    await sm.close()

    again = SessionManager(tmp_path / "test.db")
    await again.initialize()
    try:
        for user in ("a", "b"):
            reloaded = await again.get_or_create(user, "web")
            assert reloaded.metadata.get(ONBOARDING_PROMPT_SENT_KEY) == "true", user
    finally:
        await again.close()


async def test_a_hit_during_the_eviction_save_keeps_the_session_cached(capped: SessionManager):
    """A request for the session being evicted, while its save is awaited,
    gets the cached object, not the row as it was before the write."""
    a = await capped.get_or_create("a", "web")
    a.metadata[ONBOARDING_PROMPT_SENT_KEY] = "true"  # in memory only, until the eviction saves it
    await capped.get_or_create("b", "web")
    hits = []

    async def hit_a(session_id: str) -> None:
        if session_id == "web:a" and not hits:
            hits.append(await capped.get_or_create("a", "web"))

    _on_save(capped, hit_a)
    await capped.get_or_create("c", "web")

    assert hits[0] is a, "the hit during the save loaded a second copy of web:a"
    assert capped._cache.get("web:a") is a
    assert list(capped._cache) == ["web:c", "web:a"]


async def test_a_failed_eviction_save_keeps_the_session_and_fails_nothing(
    capped: SessionManager, caplog: pytest.LogCaptureFixture
):
    a = await capped.get_or_create("a", "web")
    a.metadata[ONBOARDING_PROMPT_SENT_KEY] = "true"
    await capped.get_or_create("b", "web")
    failures = []

    async def fail_a_once(session_id: str) -> None:
        if session_id == "web:a" and not failures:
            failures.append(session_id)
            raise OSError("database is locked")

    _on_save(capped, fail_a_once)
    c = await capped.get_or_create("c", "web")  # the request for c still succeeds

    assert c.session_id == "web:c"
    assert list(capped._cache) == ["web:c", "web:a"]  # a kept and moved up; b went instead
    assert capped._cache["web:a"] is a
    assert "web:a" in caplog.text

    await capped.get_or_create("d", "web")  # drops c
    await capped.get_or_create("e", "web")  # a is the oldest again, and its save now succeeds
    assert "web:a" not in capped._cache

    reloaded = await capped.get_or_create("a", "web")
    assert reloaded is not a
    assert reloaded.metadata.get(ONBOARDING_PROMPT_SENT_KEY) == "true"


async def _stored(tmp_path: Path, session_id: str):
    service = await SessionService.for_path(tmp_path / "test.db")
    return await service.load(session_id, user_id="", channel="", legacy_fallback=False)


async def test_an_unchanged_session_leaves_no_row_when_evicted(tmp_path: Path):
    """Loaded for a look and never changed: evicting it writes nothing."""
    sm = SessionManager(tmp_path / "test.db", max_cached=1)
    await sm.initialize()
    try:
        await sm.get_or_create("a", "web")
        await sm.get_or_create("b", "web")
        assert "web:a" not in sm._cache

        assert await _stored(tmp_path, "web:a") is None
    finally:
        await sm.close()


async def test_close_leaves_no_row_for_an_unchanged_session(tmp_path: Path):
    sm = SessionManager(tmp_path / "test.db")
    await sm.initialize()
    await sm.get_or_create("a", "web")
    await sm.close()

    assert await _stored(tmp_path, "web:a") is None


def _always_fail(*keys: str):
    """A save hook that fails for `failing` every time, counting the attempts.
    Clear `failing` before the manager closes, or close() fails too."""
    attempts: list[str] = []
    failing = set(keys)

    async def hook(session_id: str) -> None:
        attempts.append(session_id)
        if session_id in failing:
            raise OSError("database is locked")

    return hook, attempts, failing


async def test_a_session_that_cannot_be_saved_does_not_block_eviction(capped: SessionManager):
    a = await capped.get_or_create("a", "web")
    a.metadata[ONBOARDING_PROMPT_SENT_KEY] = "true"
    await capped.get_or_create("b", "web")
    hook, _, failing = _always_fail("web:a")
    _on_save(capped, hook)
    try:
        c = await capped.get_or_create("c", "web")

        assert c.session_id == "web:c"
        assert list(capped._cache) == ["web:c", "web:a"]  # b evicted in a's place
        assert capped._cache["web:a"] is a
    finally:
        failing.clear()


async def test_eviction_attempts_are_bounded_per_request(tmp_path: Path):
    """Four sessions that cannot be saved, one over the cap: the request
    tries one plus two spare, not all four, and still gets its session."""
    sm = SessionManager(tmp_path / "test.db", max_cached=4)
    await sm.initialize()
    for user in ("a", "b", "c", "e"):
        (await sm.get_or_create(user, "web")).metadata[ONBOARDING_PROMPT_SENT_KEY] = "true"
    hook, attempts, failing = _always_fail("web:a", "web:b", "web:c", "web:e")
    _on_save(sm, hook)
    try:
        d = await sm.get_or_create("d", "web")

        assert attempts == ["web:a", "web:b", "web:c"]
        assert sm._cache.get("web:d") is d
    finally:
        failing.clear()
        await sm.close()


async def test_a_repeatedly_failing_session_is_warned_about_once(
    capped: SessionManager, caplog: pytest.LogCaptureFixture
):
    a = await capped.get_or_create("a", "web")
    a.metadata[ONBOARDING_PROMPT_SENT_KEY] = "true"
    await capped.get_or_create("b", "web")
    hook, attempts, failing = _always_fail("web:a")
    _on_save(capped, hook)
    try:
        for user in ("c", "d", "e"):  # a is the oldest again on c and on e
            await capped.get_or_create(user, "web")

        assert attempts.count("web:a") == 2
        warnings = [r for r in caplog.records if r.levelname == "WARNING" and "web:a" in r.getMessage()]
        assert len(warnings) == 1
    finally:
        failing.clear()


async def test_the_requested_session_is_never_the_one_evicted(capped: SessionManager):
    """Both older sessions fail and move up, leaving the new key oldest: it
    stays cached, rather than being dropped while its request uses it."""
    for user in ("a", "b"):
        (await capped.get_or_create(user, "web")).metadata[ONBOARDING_PROMPT_SENT_KEY] = "true"
    hook, attempts, failing = _always_fail("web:a", "web:b")
    _on_save(capped, hook)
    try:
        c = await capped.get_or_create("c", "web")

        assert attempts == ["web:a", "web:b"]
        assert capped._cache.get("web:c") is c
    finally:
        failing.clear()


async def test_active_turn_is_not_evicted(session_manager):
    session_manager._max_cached = 1
    async with session_manager.turn("user", "chat"):
        original = await session_manager.get_or_create("user", "chat")
        await session_manager.get_or_create("other", "chat")
        assert await session_manager.get_or_create("user", "chat") is original


async def test_turn_uses_same_unified_key_as_session(session_manager):
    async with session_manager.turn("user", "chat", pincer_user_id="canonical"):
        session = await session_manager.get_or_create("user", "chat", pincer_user_id="canonical")
        assert session.session_id in session_manager._turn_locks


async def test_append_note_adds_an_assistant_message_to_the_exact_session(session_manager: SessionManager):
    """A call report lands in the session the call was asked in, after the turn that asked for it."""
    key = conversation_key("u", "web", CONVERSATION)
    session = await session_manager.get_or_create("u", "web", session_key=key)
    await session_manager.add_message(session, LLMMessage(role=MessageRole.USER, content="call the dentist"))
    await session_manager.add_message(session, LLMMessage(role=MessageRole.ASSISTANT, content="It's ringing."))
    await session_manager.evict(key)

    await session_manager.append_note(key, "u", "web", "📞 Call report (Dentist):\nNo answer.")

    reloaded = await session_manager.get_or_create("u", "web", session_key=key)
    assert [(m.role, m.content) for m in reloaded.messages][-1] == (
        MessageRole.ASSISTANT,
        "📞 Call report (Dentist):\nNo answer.",
    )
    main = await session_manager.get_or_create("u", "web")
    assert main.messages == []


async def test_append_note_waits_for_a_turn_in_progress(session_manager: SessionManager):
    """The note goes in after the running turn's own messages, never between them."""
    import asyncio

    key = "telegram:u"
    async with session_manager.turn("u", "telegram", session_key=key):
        note = asyncio.create_task(session_manager.append_note(key, "u", "telegram", "report"))
        await asyncio.sleep(0.01)
        session = await session_manager.get_or_create("u", "telegram", session_key=key)
        await session_manager.add_message(session, LLMMessage(role=MessageRole.ASSISTANT, content="turn reply"))
    await note
    session = await session_manager.get_or_create("u", "telegram", session_key=key)
    assert [m.content for m in session.messages] == ["turn reply", "report"]


def test_a_call_report_after_the_reply_serializes_for_the_next_turn():
    """Report + previous reply are two assistant messages in a row: Anthropic
    gets them merged into one, OpenAI as they are, and the next user turn follows."""
    from types import SimpleNamespace

    from pincer.llm.anthropic_common import AnthropicCompatibleProvider
    from pincer.llm.openai_common import convert_messages_to_openai

    history = [
        LLMMessage(role=MessageRole.USER, content="call the dentist"),
        LLMMessage(role=MessageRole.ASSISTANT, content="It's ringing."),
        LLMMessage(role=MessageRole.ASSISTANT, content="📞 Call report (Dentist):\nNo answer."),
        LLMMessage(role=MessageRole.USER, content="did they answer?"),
    ]

    anthropic_self = SimpleNamespace(
        _validate_api_messages=AnthropicCompatibleProvider._validate_api_messages, _provider_name="anthropic"
    )
    anthropic = AnthropicCompatibleProvider._convert_messages(anthropic_self, history, "claude-x")  # type: ignore[arg-type]
    assert [m["role"] for m in anthropic] == ["user", "assistant", "user"]
    assert "No answer." in anthropic[1]["content"]
    assert "It's ringing." in anthropic[1]["content"]

    openai = convert_messages_to_openai(history)
    assert [m["role"] for m in openai] == ["user", "assistant", "assistant", "user"]


_THOUGHT = [{"type": "thinking", "thinking": "SECRET-THOUGHT", "signature": "SIG"}]


async def _fill(session_manager: SessionManager, session) -> None:
    """25 messages, past the old cap of 20 of the fixture; the last carries an image, assistant ones a thought."""
    for index in range(25):
        role = MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT
        await session_manager.add_message(
            session,
            LLMMessage(
                role=role,
                content=f"message {index}",
                images=[ImageContent(data="aW1n", media_type="image/png")] if index == 24 else [],
                raw=list(_THOUGHT) if role is MessageRole.ASSISTANT else [],
            ),
        )


def _assert_whole(session) -> None:
    assert [m.content for m in session.messages] == [f"message {i}" for i in range(25)]  # nothing is cut
    assert [len(m.images) for m in session.messages] == [0] * 24 + [1]  # the photo stays
    assert all(m.raw == _THOUGHT for m in session.messages if m.role is MessageRole.ASSISTANT)  # so does thinking


async def test_a_turn_keeps_its_whole_history_thinking_and_photos(session_manager):
    async with session_manager.turn("user1", "test") as session:
        await _fill(session_manager, session)
        _assert_whole(session)

    _assert_whole(session)  # and when the turn ends


async def test_a_failed_turn_keeps_its_messages_and_releases_the_session(session_manager):
    with pytest.raises(RuntimeError, match="boom"):
        async with session_manager.turn("user1", "test") as session:
            await _fill(session_manager, session)
            raise RuntimeError("boom")

    _assert_whole(session)
    async with asyncio.timeout(5):  # the turn lock is free again
        async with session_manager.turn("user1", "test") as again:
            assert again is session


async def test_a_cancelled_turn_keeps_its_messages_and_releases_the_session(session_manager):
    inside = asyncio.Event()
    holder: list = []

    async def run() -> None:
        async with session_manager.turn("user1", "test") as session:
            holder.append(session)
            await _fill(session_manager, session)
            inside.set()
            await asyncio.sleep(60)

    task = asyncio.create_task(run())
    await asyncio.wait_for(inside.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    session = holder[0]
    _assert_whole(session)
    async with asyncio.timeout(5):
        async with session_manager.turn("user1", "test") as again:
            assert again is session


async def test_a_turn_yields_the_cached_session(session_manager):
    async with session_manager.turn("user1", "test") as session:
        assert await session_manager.get_or_create("user1", "test") is session
        assert session.session_id in session_manager._turn_locks


# ── the cache is bounded by the photos it holds, not only by how many sessions (PR #379 review, round 2) ──

_PHOTO_BYTES = 3_000  # 4,000 base64 characters


async def _with_photo(sm: SessionManager, user: str) -> None:
    """A session of `user` whose one message shows a photo of its own."""
    session = await sm.get_or_create(user, "web")
    photo = ImageContent.from_bytes(user.encode() * (_PHOTO_BYTES // len(user)), "image/jpeg")
    await sm.add_message(session, LLMMessage(role=MessageRole.USER, content="look", images=[photo]))


@pytest.fixture
async def photo_budget(tmp_path: Path):
    """A manager that caches many sessions but holds the photos of about two of them."""
    sm = SessionManager(tmp_path / "test.db", max_cached=100, max_cached_photo_bytes=9_000)
    await sm.initialize()
    yield sm
    await sm.close()


async def test_the_cache_drops_the_least_recently_used_session_when_its_photos_pass_the_budget(
    photo_budget: SessionManager,
):
    for user in ("a", "b", "c"):
        await _with_photo(photo_budget, user)  # 3 x 4,000 characters: over the 9,000
    assert len(photo_budget._cache) == 3  # precondition: only the count cap (100) applies so far

    await photo_budget.get_or_create("d", "web")

    assert list(photo_budget._cache) == ["web:b", "web:c", "web:d"]


async def test_an_evicted_session_reloads_with_its_photo(photo_budget: SessionManager):
    for user in ("a", "b", "c"):
        await _with_photo(photo_budget, user)
    await photo_budget.get_or_create("d", "web")
    assert "web:a" not in photo_budget._cache

    again = await photo_budget.get_or_create("a", "web")

    assert [len(m.images) for m in again.messages] == [1]
    assert again.messages[0].images[0].data  # the bytes are back


async def test_a_session_in_a_turn_is_not_evicted_for_its_photos(photo_budget: SessionManager):
    for user in ("a", "b", "c"):
        await _with_photo(photo_budget, user)

    async with photo_budget._locked("web:a"):  # a turn on a: it is the least recently used, and pinned
        await photo_budget.get_or_create("d", "web")

        assert sorted(photo_budget._cache) == ["web:a", "web:c", "web:d"]  # b went instead


async def test_the_session_being_loaded_stays_even_when_its_own_photos_pass_the_budget(tmp_path: Path):
    sm = SessionManager(tmp_path / "test.db", max_cached=100, max_cached_photo_bytes=1_000)
    await sm.initialize()
    try:
        await _with_photo(sm, "a")
        await sm.evict("web:a")

        again = await sm.get_or_create("a", "web")

        assert sm._cache["web:a"] is again
        assert len(again.messages[0].images) == 1
    finally:
        await sm.close()


async def test_a_photo_two_messages_show_counts_once(photo_budget: SessionManager):
    session = await photo_budget.get_or_create("a", "web")
    photo = ImageContent.from_bytes(b"x" * _PHOTO_BYTES, "image/jpeg")
    for _ in range(3):
        await photo_budget.add_message(session, LLMMessage(role=MessageRole.USER, content="again", images=[photo]))

    assert session.photo_bytes() == len(photo.data)


# ── eviction for photos frees photos, and stops when only the session being used holds them (round 3) ──


async def _plain(sm: SessionManager, user: str) -> None:
    """A session of `user` with one message and no photo (so it has something to save when evicted)."""
    session = await sm.get_or_create(user, "web")
    await sm.add_message(session, LLMMessage(role=MessageRole.USER, content="hello"))


async def _with_photos(sm: SessionManager, user: str, count: int) -> None:
    session = await sm.get_or_create(user, "web")
    for n in range(count):
        photo = ImageContent.from_bytes(f"{user}{n}".encode() * (_PHOTO_BYTES // 2), "image/jpeg")
        await sm.add_message(session, LLMMessage(role=MessageRole.USER, content="look", images=[photo]))


def _count_saves(sm: SessionManager) -> list[str]:
    saved: list[str] = []
    real = sm._store.save

    async def spy(**kwargs):
        saved.append(kwargs["session_id"])
        return await real(**kwargs)

    sm._store.save = spy  # type: ignore[method-assign]
    return saved


async def test_the_session_being_loaded_holding_all_the_photos_does_not_empty_the_cache(photo_budget: SessionManager):
    await _with_photos(photo_budget, "big", 3)  # 3 x 4,000 characters: over the 9,000 on its own
    await photo_budget.evict("web:big")
    for user in ("p1", "p2", "p3", "p4", "p5"):
        await _plain(photo_budget, user)
    saved = _count_saves(photo_budget)

    big = await photo_budget.get_or_create("big", "web")  # a miss: loaded with its photos, over the budget

    assert list(photo_budget._cache) == ["web:p1", "web:p2", "web:p3", "web:p4", "web:p5", "web:big"]
    assert photo_budget._cache["web:big"] is big
    assert saved == []  # nobody was saved or dropped for photos they do not hold


async def test_a_session_in_a_turn_holding_all_the_photos_does_not_empty_the_cache(photo_budget: SessionManager):
    big = await photo_budget.get_or_create("big", "web")  # the oldest
    for user in ("p1", "p2", "p3"):
        await _plain(photo_budget, user)
    for n in range(3):  # photos arrive in its turn: 12,000 characters, over the 9,000
        photo = ImageContent.from_bytes(f"big{n}".encode() * (_PHOTO_BYTES // 2), "image/jpeg")
        await photo_budget.add_message(big, LLMMessage(role=MessageRole.USER, content="look", images=[photo]))
    saved = _count_saves(photo_budget)

    async with photo_budget._locked("web:big"):  # in a turn: it cannot go
        await photo_budget.get_or_create("d", "web")

    assert sorted(photo_budget._cache) == ["web:big", "web:d", "web:p1", "web:p2", "web:p3"]
    assert saved == []


async def test_eviction_for_photos_takes_the_oldest_session_that_holds_some_and_leaves_the_others(
    photo_budget: SessionManager,
):
    await _plain(photo_budget, "p")  # the oldest, no photo
    for user in ("a", "b", "c"):
        await _with_photos(photo_budget, user, 1)  # 12,000 characters in all, over the 9,000

    await photo_budget.get_or_create("d", "web")

    assert list(photo_budget._cache) == ["web:p", "web:b", "web:c", "web:d"]


async def test_a_cache_miss_does_not_walk_the_messages_of_the_cached_sessions(
    photo_budget: SessionManager, monkeypatch: pytest.MonkeyPatch
):
    """The photo total is kept from what each session's last save counted, not summed over every message of every
    cached session on every miss (PR #379 review, round 3)."""
    from pincer.core.session import Session

    for n in range(20):
        await _plain(photo_budget, f"u{n}")
    walks = 0
    real = Session.photo_bytes

    def counting(self: Session) -> int:
        nonlocal walks
        walks += 1
        return real(self)

    monkeypatch.setattr(Session, "photo_bytes", counting)

    await photo_budget.get_or_create("new", "web")

    assert walks <= 1  # the session just loaded, not the 20 cached


async def test_the_photo_total_follows_a_clear_and_a_cut(photo_budget: SessionManager):
    sessions = {}
    for user in ("a", "b", "c"):
        await _with_photos(photo_budget, user, 1)  # 12,000 characters in all, over the 9,000
        sessions[user] = photo_budget._cache[f"web:{user}"]
    # a clear and a cut free two photos: 4,000 left, within the budget
    await photo_budget.clear(sessions["a"])
    await photo_budget.cut(sessions["b"], list(sessions["b"].messages))

    await photo_budget.get_or_create("d", "web")

    assert list(photo_budget._cache) == ["web:a", "web:b", "web:c", "web:d"]  # nothing went


# ── eviction stops when it cannot help, and walks the cache once (PR #379 review, round 4) ──


async def test_other_photo_sessions_stay_when_the_session_being_loaded_is_over_the_budget_by_itself(
    photo_budget: SessionManager,
):
    await _with_photos(photo_budget, "big", 3)  # 12,000 characters: over the 9,000 on its own
    await photo_budget.evict("web:big")
    for user in ("a", "b"):
        await _with_photos(photo_budget, user, 1)  # 4,000 each: 8,000 together, under the budget
    assert list(photo_budget._cache) == ["web:a", "web:b"]  # precondition: nothing was evicted so far

    await photo_budget.get_or_create("big", "web")  # their eviction could not bring the total under 9,000

    assert list(photo_budget._cache) == ["web:a", "web:b", "web:big"]


async def test_other_photo_sessions_stay_when_a_session_in_a_turn_is_over_the_budget_by_itself(
    photo_budget: SessionManager,
):
    for user in ("a", "b"):
        await _with_photos(photo_budget, user, 1)
    big = await photo_budget.get_or_create("big", "web")
    for n in range(3):  # photos arrive in its turn: 12,000 characters
        photo = ImageContent.from_bytes(f"big{n}".encode() * (_PHOTO_BYTES // 2), "image/jpeg")
        await photo_budget.add_message(big, LLMMessage(role=MessageRole.USER, content="look", images=[photo]))

    async with photo_budget._locked("web:big"):
        await photo_budget.get_or_create("d", "web")

    assert sorted(photo_budget._cache) == ["web:a", "web:b", "web:big", "web:d"]


async def test_the_sessions_dropped_for_photos_are_found_in_one_walk_of_the_cache(tmp_path: Path):
    """With many sessions that hold no photo at the front of the cache, every step of an eviction that looked for
    the next photo session from the front walked all of them again. The cache is iterated through a counting
    subclass (its own entry points, so the bound does not depend on which of them the code calls)."""

    class Counting(OrderedDict[str, Any]):
        steps = 0

        def _count(self, entries):
            for entry in entries:
                type(self).steps += 1
                yield entry

        def __iter__(self):
            return self._count(super().__iter__())

        def items(self):  # type: ignore[override]
            return self._count(super().items())

        def keys(self):  # type: ignore[override]
            return self._count(super().keys())

        def values(self):  # type: ignore[override]
            return self._count(super().values())

    sm = SessionManager(tmp_path / "test.db", max_cached=1_000, max_cached_photo_bytes=10**9)
    await sm.initialize()
    try:
        for n in range(300):
            await _plain(sm, f"plain{n}")  # the oldest: no photo
        for n in range(40):
            await _with_photos(sm, f"q{n:02d}", 1)  # 8,000 characters each, 320,000 in all
        assert len(sm._cache) == 340  # precondition: every one is still cached (the budget was out of reach)
        sm._max_cached_photo_bytes = 9_000  # the budget drops under what is cached (the one private poke)
        sm._cache = Counting(sm._cache)  # type: ignore[assignment]
        Counting.steps = 0

        await sm.get_or_create("new", "web")

        photo_sessions = [key for key in sm._cache if key.startswith("web:q")]
        assert photo_sessions == ["web:q39"]  # must-reach control: the photos were brought down to the budget (9,000)
        assert Counting.steps < 4 * 340  # a walk or two of the 340, not one per dropped session (12,000+ before)
    finally:
        await sm.close()
