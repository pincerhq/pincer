"""SessionManager and the session key format."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pincer.core.session import ONBOARDING_PROMPT_SENT_KEY, SessionManager, conversation_key
from pincer.llm.base import LLMMessage, MessageRole

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
    a.mark_onboarding_prompt_sent()  # in memory only: no add_message after it
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
        (await sm.get_or_create(user, "web")).mark_onboarding_prompt_sent()
    await sm.close()

    again = SessionManager(tmp_path / "test.db", max_cached=2)
    await again.initialize()
    try:
        for user in ("a", "b"):
            reloaded = await again.get_or_create(user, "web")
            assert reloaded.metadata.get(ONBOARDING_PROMPT_SENT_KEY) == "true", user
    finally:
        await again.close()
