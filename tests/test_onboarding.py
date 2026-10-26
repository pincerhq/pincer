"""Tests for first-session onboarding."""

from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from pincer.core.agent import Agent, StreamEventType
from pincer.core.onboarding import ONBOARDING_FOLLOWUP_INSTRUCTION, ONBOARDING_QUESTION_EN
from pincer.core.session import (
    ONBOARDING_COMPLETE_KEY,
    ONBOARDING_PROMPT_SENT_KEY,
    conversation_key,
)
from pincer.db.engine import get_database_url
from pincer.llm.base import LLMMessage, LLMResponse, MessageRole
from pincer.memory.store import MemoryStore
from pincer.services.onboarding import OnboardingRecord, OnboardingService


@pytest_asyncio.fixture
async def memory_store(tmp_path: Path) -> MemoryStore:
    store = MemoryStore(tmp_path / "mem.db")
    await store.initialize()
    yield store  # type: ignore[misc]
    await store.close()


def _llm_text(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        model="test-model",
        input_tokens=10,
        output_tokens=10,
        stop_reason="end_turn",
    )


def _streams(llm: AsyncMock, text: str) -> None:
    """Give the mock LLM a stream that yields ``text``, as a provider would."""

    async def stream(**_: object) -> AsyncIterator[str]:
        yield text

    llm.stream = stream


def _scripted_llm(*replies: str | Exception) -> AsyncMock:
    """An LLM whose complete() answers in order; its stream replays nothing new."""
    llm = AsyncMock()
    llm.complete.side_effect = [r if isinstance(r, Exception) else _llm_text(r) for r in replies]
    _streams(llm, "Streamed reply.")
    return llm


async def _reply(
    agent: Agent,
    path: str,
    user_id: str,
    channel: str,
    text: str,
    *,
    session_key: str | None = None,
    channel_name: str | None = None,
) -> str:
    """One turn on the blocking or the streaming path; the reply the user sees."""
    if path == "blocking":
        result = await agent.handle_message(user_id, channel, text, channel_name=channel_name, session_key=session_key)
        return result.text
    chunks = [c async for c in agent.handle_message_stream(user_id, channel, text, session_key=session_key)]
    return [c.content for c in chunks if c.type == StreamEventType.DONE][-1]


async def _record(tmp_path: Path, user_id: str, channel: str) -> OnboardingRecord | None:
    """The stored onboarding row, read past every cache (the fixture's db is at head)."""
    return await OnboardingService(get_database_url(tmp_path / "test.db")).get(user_id, channel)


async def _store_history(session_manager, user_id: str, channel: str, *, session_key: str | None = None, **metadata):
    session = await session_manager.get_or_create(user_id, channel, session_key=session_key)
    session.metadata.update(metadata)
    await session_manager.add_message(session, LLMMessage(role=MessageRole.USER, content="earlier message"))
    await session_manager.add_message(session, LLMMessage(role=MessageRole.ASSISTANT, content="earlier reply"))


_PATHS = pytest.mark.parametrize("path", ["blocking", "stream"])


async def test_fresh_session_appends_question(
    tmp_path, settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """First message in a fresh session: response must end with the question."""
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    result = await agent.handle_message("user-fresh", "test", "Hi!")

    assert result.text.endswith(ONBOARDING_QUESTION_EN)
    record = await _record(tmp_path, "user-fresh", "test")
    assert record is not None
    assert (record.asked_session_id, record.completed_at) == ("test:user-fresh", None)


@_PATHS
async def test_first_message_in_conversation_prompts_and_records_asking_session(
    path, tmp_path, settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """A new user whose very first message is in a conversation is asked there."""
    _streams(mock_llm, "Hello! I'm Pincer.")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    key = conversation_key("user-new", "web", "conv-1")

    reply = await _reply(agent, path, "user-new", "web", "Hi!", session_key=key)

    assert reply.endswith(ONBOARDING_QUESTION_EN)
    record = await _record(tmp_path, "user-new", "web")
    assert record is not None
    assert (record.asked_session_id, record.completed_at) == (key, None)
    conversation = await session_manager.get_or_create("user-new", "web", session_key=key)
    assert [m.content for m in conversation.messages if m.role == MessageRole.USER] == ["Hi!"]
    assert ONBOARDING_PROMPT_SENT_KEY not in conversation.metadata


@_PATHS
async def test_reply_in_asking_session_completes_onboarding(
    path, tmp_path, settings, session_manager, cost_tracker, tool_registry, memory_store
):
    llm = _scripted_llm(
        "Hello!", "Nice to meet you, Bob.", '{"name": "Bob", "use_case": "accounting", "language": "en"}', "Sure."
    )
    agent = Agent(settings, llm, session_manager, cost_tracker, tool_registry, memory_store=memory_store)
    key = conversation_key("user-b", "web", "x")

    await _reply(agent, path, "user-b", "web", "Hi!", session_key=key)
    await _reply(agent, path, "user-b", "web", "I'm Bob, accountant", session_key=key)

    record = await _record(tmp_path, "user-b", "web")
    assert record is not None
    assert record.completed_at is not None
    assert ONBOARDING_FOLLOWUP_INSTRUCTION in llm.complete.call_args_list[1].kwargs["system"]
    profiles = await memory_store.list_memories(user_id="user-b", category="profile")
    assert [("Bob" in p.content) for p in profiles] == [True]

    await _reply(agent, path, "user-b", "web", "what's the weather?", session_key=key)

    assert llm.complete.call_count == 4
    assert ONBOARDING_FOLLOWUP_INSTRUCTION not in llm.complete.call_args_list[3].kwargs["system"]


@_PATHS
async def test_first_message_in_another_session_is_not_the_answer(
    path, tmp_path, settings, session_manager, cost_tracker, tool_registry
):
    """Asked in the main chat, a task sent in a new one is not taken for the
    answer; the next main-chat message is."""
    llm = _scripted_llm("Hello!", "Here is your summary.", "Nice to meet you, Bob.", '{"name": "Bob"}')
    agent = Agent(settings, llm, session_manager, cost_tracker, tool_registry)

    await _reply(agent, path, "user-b", "web", "Hi!")
    reply = await _reply(
        agent, path, "user-b", "web", "summarize my emails", session_key=conversation_key("user-b", "web", "x")
    )

    assert ONBOARDING_QUESTION_EN not in reply
    assert ONBOARDING_FOLLOWUP_INSTRUCTION not in llm.complete.call_args_list[1].kwargs["system"]
    assert llm.complete.call_count == 2
    record = await _record(tmp_path, "user-b", "web")
    assert record is not None
    assert (record.asked_session_id, record.completed_at) == ("web:user-b", None)

    await _reply(agent, path, "user-b", "web", "I'm Bob")

    assert ONBOARDING_FOLLOWUP_INSTRUCTION in llm.complete.call_args_list[2].kwargs["system"]
    assert llm.complete.call_count == 4
    record = await _record(tmp_path, "user-b", "web")
    assert record is not None
    assert record.completed_at is not None


@_PATHS
async def test_existing_user_new_conversation_is_separate_and_not_prompted(
    path, tmp_path, settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """A web chat conversation is its own session, and a user with history in
    their main one is not asked who they are when they open "New chat"."""
    _streams(mock_llm, "Hello! I'm Pincer.")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    await _store_history(session_manager, "user-t", "web")
    key = conversation_key("user-t", "web", "thread-1")

    reply = await _reply(agent, path, "user-t", "web", "Hi!", session_key=key)

    assert reply == "Hello! I'm Pincer."
    thread = await session_manager.get_or_create("user-t", "web", session_key=key)
    assert [m.content for m in thread.messages if m.role == MessageRole.USER] == ["Hi!"]
    main = await session_manager.get_or_create("user-t", "web")
    assert [m.content for m in main.messages if m.role == MessageRole.USER] == ["earlier message"]
    assert await _record(tmp_path, "user-t", "web") is None


@_PATHS
async def test_history_only_in_conversations_is_not_new(
    path, tmp_path, settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """No main-chat history, but a stored conversation: not a new user."""
    _streams(mock_llm, "Hello! I'm Pincer.")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    await _store_history(session_manager, "user-c", "web", session_key=conversation_key("user-c", "web", "a"))

    reply = await _reply(agent, path, "user-c", "web", "Hi!", session_key=conversation_key("user-c", "web", "b"))

    assert reply == "Hello! I'm Pincer."
    assert await _record(tmp_path, "user-c", "web") is None


@_PATHS
async def test_legacy_pending_prompt_ingests_reply_in_that_session(
    path, tmp_path, settings, session_manager, cost_tracker, tool_registry
):
    """Asked before the table existed: the flag on the session still routes
    the reply, and closes the gate in the table."""
    llm = _scripted_llm("Nice to meet you, Dana.", '{"name": "Dana"}')
    agent = Agent(settings, llm, session_manager, cost_tracker, tool_registry)
    await _store_history(session_manager, "user-l", "web", **{ONBOARDING_PROMPT_SENT_KEY: "true"})

    await _reply(agent, path, "user-l", "web", "I'm Dana")

    assert ONBOARDING_FOLLOWUP_INSTRUCTION in llm.complete.call_args_list[0].kwargs["system"]
    assert llm.complete.call_count == 2
    record = await _record(tmp_path, "user-l", "web")
    assert record is not None
    assert record.completed_at is not None


@_PATHS
async def test_legacy_onboarded_session_is_not_asked(
    path, tmp_path, settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """Onboarded before the table existed: never asked again, nothing ingested."""
    _streams(mock_llm, "Hello! I'm Pincer.")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    await _store_history(
        session_manager, "user-o", "web", **{ONBOARDING_PROMPT_SENT_KEY: "true", ONBOARDING_COMPLETE_KEY: "true"}
    )

    reply = await _reply(agent, path, "user-o", "web", "hello again")

    assert reply == "Hello! I'm Pincer."
    assert mock_llm.complete.call_count == 1
    assert ONBOARDING_FOLLOWUP_INSTRUCTION not in mock_llm.complete.call_args.kwargs["system"]
    assert await _record(tmp_path, "user-o", "web") is None


@_PATHS
async def test_a_turn_that_loses_the_claim_does_not_ask(
    path, tmp_path, settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """Two first messages at once both decide to ask; only the one whose claim
    lands appends the question."""
    _streams(mock_llm, "Hello! I'm Pincer.")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    other = conversation_key("user-r", "web", "a")
    assert await session_manager.onboarding.claim_prompt("user-r", "web", other, now=1.0)
    stored = session_manager.onboarding.get
    session_manager.onboarding.get = AsyncMock(return_value=None)  # decided before the other turn's claim

    reply = await _reply(agent, path, "user-r", "web", "Hi!", session_key=conversation_key("user-r", "web", "b"))

    session_manager.onboarding.get = stored
    assert reply == "Hello! I'm Pincer."
    record = await _record(tmp_path, "user-r", "web")
    assert record is not None
    assert record.asked_session_id == other


@pytest.mark.parametrize("session_key", [None, "voice:CA123"], ids=["no_key", "call_key"])
@_PATHS
async def test_voice_never_onboarded(
    path, session_key, tmp_path, settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """Nobody onboards mid-phone-call, with or without a per-call session."""
    _streams(mock_llm, "Hello! I'm Pincer.")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    reply = await _reply(agent, path, "+15550100", "voice", "Hi!", session_key=session_key, channel_name="voice")

    assert reply == "Hello! I'm Pincer."
    assert await _record(tmp_path, "+15550100", "voice") is None


async def test_thread_channels_onboard_per_thread(
    tmp_path, settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """Slack/Teams split sessions through ``channel``: each thread is its own scope."""
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    first = await agent.handle_message("U1", "slack-thread-1", "Hi!", channel_name="slack")
    second = await agent.handle_message("U1", "slack-thread-2", "Hi!", channel_name="slack")

    assert first.text.endswith(ONBOARDING_QUESTION_EN)
    assert second.text.endswith(ONBOARDING_QUESTION_EN)
    assert await _record(tmp_path, "U1", "slack-thread-2") is not None


async def test_second_message_closes_gate_and_extracts_fields(
    tmp_path, settings, session_manager, cost_tracker, tool_registry, memory_store
):
    """Second message: gate closes, fields are extracted into the profile memory."""
    llm = _scripted_llm(
        "Hello!",
        "Nice to meet you, Alice.",
        '{"name": "Alice", "use_case": "drafting marketing emails", "language": "en"}',
    )
    agent = Agent(settings, llm, session_manager, cost_tracker, tool_registry, memory_store=memory_store)

    await agent.handle_message("user-2", "test", "Hi!")
    await agent.handle_message("user-2", "test", "I'm Alice, I want help drafting marketing emails, in English.")

    record = await _record(tmp_path, "user-2", "test")
    assert record is not None
    assert record.completed_at is not None
    session = await session_manager.get_or_create("user-2", "test")
    assert ONBOARDING_COMPLETE_KEY not in session.metadata

    profiles = await memory_store.list_memories(user_id="user-2", category="profile")
    assert len(profiles) == 1
    assert "Alice" in profiles[0].content
    assert "marketing" in profiles[0].content


async def test_second_message_gibberish_still_closes_gate(
    tmp_path, settings, session_manager, cost_tracker, tool_registry, memory_store
):
    """Even an unparseable reply must close the gate — no re-prompting."""
    llm = _scripted_llm("Hi!", "Got it.", "not json")
    agent = Agent(settings, llm, session_manager, cost_tracker, tool_registry, memory_store=memory_store)

    await agent.handle_message("user-3", "test", "hello")
    await agent.handle_message("user-3", "test", "asdf")

    record = await _record(tmp_path, "user-3", "test")
    assert record is not None
    assert record.completed_at is not None

    # No profile memory written when nothing was extracted.
    profiles = await memory_store.list_memories(user_id="user-3", category="profile")
    assert profiles == []


async def test_third_message_no_question_appended(settings, session_manager, cost_tracker, tool_registry, memory_store):
    llm = _scripted_llm("Hi!", "Got it.", '{"name": "Bob", "use_case": null, "language": "en"}', "Sure thing.")
    agent = Agent(settings, llm, session_manager, cost_tracker, tool_registry, memory_store=memory_store)

    await agent.handle_message("user-4", "test", "hi")
    await agent.handle_message("user-4", "test", "I'm Bob")
    result3 = await agent.handle_message("user-4", "test", "what's the weather?")

    assert ONBOARDING_QUESTION_EN not in result3.text


async def test_preexisting_session_never_prompts(
    tmp_path, settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """Users with prior messages never see the onboarding question."""
    await _store_history(session_manager, "user-old", "test")

    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    result = await agent.handle_message("user-old", "test", "hello again")

    assert ONBOARDING_QUESTION_EN not in result.text
    assert await _record(tmp_path, "user-old", "test") is None


async def test_extraction_failure_closes_gate(
    tmp_path, settings, session_manager, cost_tracker, tool_registry, memory_store
):
    """If extraction call raises, the gate still closes."""
    llm = _scripted_llm("Hi!", "Got it.", RuntimeError("LLM down"))
    agent = Agent(settings, llm, session_manager, cost_tracker, tool_registry, memory_store=memory_store)

    await agent.handle_message("user-5", "test", "hi")
    await agent.handle_message("user-5", "test", "I'm Carol")

    record = await _record(tmp_path, "user-5", "test")
    assert record is not None
    assert record.completed_at is not None


@pytest.mark.asyncio
async def test_add_profile_replaces_existing(memory_store):
    await memory_store.add_profile(user_id="u1", name="Alice", use_case="emails", language="en")
    await memory_store.add_profile(user_id="u1", name="Alice S.", use_case="cooking", language="en")

    profiles = await memory_store.list_memories(user_id="u1", category="profile")
    assert len(profiles) == 1
    assert "Alice S." in profiles[0].content
    assert "cooking" in profiles[0].content


@pytest.mark.asyncio
async def test_add_profile_no_fields_is_noop(memory_store):
    result = await memory_store.add_profile(user_id="u2", name=None, use_case=None, language=None)
    assert result is None
    profiles = await memory_store.list_memories(user_id="u2", category="profile")
    assert profiles == []


@pytest.mark.asyncio
async def test_safe_json_loads_strips_fences():
    from pincer.core.agent import _safe_json_loads

    assert _safe_json_loads('```json\n{"a": 1}\n```') == {"a": 1}
    assert _safe_json_loads('{"a": 1}') == {"a": 1}
    assert _safe_json_loads("not json") is None


@pytest.mark.asyncio
async def test_clean_profile_value_normalizes():
    from pincer.core.agent import _clean_profile_value

    assert _clean_profile_value("Alice") == "Alice"
    assert _clean_profile_value("  Bob  ") == "Bob"
    assert _clean_profile_value(None) is None
    assert _clean_profile_value("null") is None
    assert _clean_profile_value("N/A") is None
    assert _clean_profile_value("") is None
