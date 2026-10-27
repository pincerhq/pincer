"""SessionManager and the session key format."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pincer.core.session import ONBOARDING_PROMPT_SENT_KEY, SessionManager, conversation_key
from pincer.llm.base import LLMMessage, MessageRole
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
