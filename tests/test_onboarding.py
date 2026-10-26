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
    PROFILE_LANGUAGE_KEY,
    PROFILE_NAME_KEY,
    PROFILE_USE_CASE_KEY,
    SessionManager,
    conversation_key,
)
from pincer.llm.base import LLMMessage, LLMResponse, MessageRole
from pincer.memory.store import MemoryStore


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


@pytest.mark.asyncio
async def test_fresh_session_appends_question(settings, mock_llm, session_manager, cost_tracker, tool_registry):
    """First message in a fresh session: response must end with the question."""
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    result = await agent.handle_message("user-fresh", "test", "Hi!")

    assert result.text.endswith(ONBOARDING_QUESTION_EN)

    session = await session_manager.get_or_create("user-fresh", "test")
    assert session.metadata.get(ONBOARDING_PROMPT_SENT_KEY) == "true"
    assert session.metadata.get(ONBOARDING_COMPLETE_KEY) != "true"


def _streams(llm: AsyncMock, text: str) -> None:
    """Give the mock LLM a stream that yields ``text``, as a provider would."""

    async def stream(**_: object) -> AsyncIterator[str]:
        yield text

    llm.stream = stream


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


@pytest.mark.parametrize("path", ["blocking", "stream"])
async def test_first_message_in_conversation_prompts_and_marks_main(
    path, tmp_path, settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """A new user whose very first message is in a conversation is asked, and
    the question is recorded on their main session, in the database."""
    _streams(mock_llm, "Hello! I'm Pincer.")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    key = conversation_key("user-new", "web", "conv-1")

    reply = await _reply(agent, path, "user-new", "web", "Hi!", session_key=key)

    assert reply.endswith(ONBOARDING_QUESTION_EN)
    conversation = await session_manager.get_or_create("user-new", "web", session_key=key)
    assert [m.content for m in conversation.messages if m.role == MessageRole.USER] == ["Hi!"]
    stored = SessionManager(tmp_path / "test.db")
    await stored.initialize()
    try:
        main = await stored.get_or_create("user-new", "web")
        assert main.metadata.get(ONBOARDING_PROMPT_SENT_KEY) == "true"
        assert main.messages == []
    finally:
        await stored.close()


@pytest.mark.parametrize("path", ["blocking", "stream"])
async def test_reply_in_new_conversation_onboards_main(path, settings, session_manager, cost_tracker, tool_registry):
    """Asked in the main chat, answered in a new one: the answer is ingested
    there, and the next main-chat message is not taken for it."""
    llm = AsyncMock()
    llm.complete.side_effect = [
        _llm_text("Hello!"),
        _llm_text("Nice to meet you, Bob."),
        _llm_text('{"name": "Bob", "use_case": "accounting", "language": "en"}'),
        _llm_text("Here is your summary."),
    ]
    _streams(llm, "Streamed reply.")
    agent = Agent(settings, llm, session_manager, cost_tracker, tool_registry)

    await _reply(agent, path, "user-b", "web", "Hi!")
    await _reply(
        agent, path, "user-b", "web", "I'm Bob, accountant", session_key=conversation_key("user-b", "web", "x")
    )

    main = await session_manager.get_or_create("user-b", "web")
    assert main.metadata.get(ONBOARDING_COMPLETE_KEY) == "true"
    assert main.metadata.get(PROFILE_NAME_KEY) == "Bob"
    assert ONBOARDING_FOLLOWUP_INSTRUCTION in llm.complete.call_args_list[1].kwargs["system"]

    await _reply(agent, path, "user-b", "web", "summarize my emails")

    assert llm.complete.call_count == 4
    assert ONBOARDING_FOLLOWUP_INSTRUCTION not in llm.complete.call_args_list[3].kwargs["system"]


@pytest.mark.parametrize("path", ["blocking", "stream"])
async def test_existing_user_new_conversation_is_separate_and_not_prompted(
    path, settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """A web chat conversation is its own session, and a user with history in
    their main one is not asked who they are when they open "New chat"."""
    _streams(mock_llm, "Hello! I'm Pincer.")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    session = await session_manager.get_or_create("user-t", "web")
    await session_manager.add_message(session, LLMMessage(role=MessageRole.USER, content="Remember the code word."))
    await session_manager.add_message(session, LLMMessage(role=MessageRole.ASSISTANT, content="Noted."))
    key = conversation_key("user-t", "web", "thread-1")

    reply = await _reply(agent, path, "user-t", "web", "Hi!", session_key=key)

    assert ONBOARDING_QUESTION_EN not in reply
    thread = await session_manager.get_or_create("user-t", "web", session_key=key)
    assert [m.content for m in thread.messages if m.role == MessageRole.USER] == ["Hi!"]
    main = await session_manager.get_or_create("user-t", "web")
    assert [m.content for m in main.messages if m.role == MessageRole.USER] == ["Remember the code word."]
    assert ONBOARDING_PROMPT_SENT_KEY not in main.metadata


@pytest.mark.parametrize("session_key", [None, "voice:CA123"], ids=["no_key", "call_key"])
@pytest.mark.parametrize("path", ["blocking", "stream"])
async def test_voice_never_onboarded(
    path, session_key, settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """Nobody onboards mid-phone-call, with or without a per-call session."""
    _streams(mock_llm, "Hello! I'm Pincer.")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    reply = await _reply(agent, path, "+15550100", "voice", "Hi!", session_key=session_key, channel_name="voice")

    assert reply == "Hello! I'm Pincer."
    main = await session_manager.get_or_create("+15550100", "voice")
    assert ONBOARDING_PROMPT_SENT_KEY not in main.metadata


@pytest.mark.asyncio
async def test_second_message_closes_gate_and_extracts_fields(
    settings, session_manager, cost_tracker, tool_registry, memory_store
):
    """Second message: gate closes, fields are extracted and persisted to metadata + memory."""
    llm = AsyncMock()
    llm.complete.side_effect = [
        _llm_text("Hello!"),  # turn 1 normal reply
        _llm_text("Nice to meet you, Alice."),  # turn 2 normal reply
        _llm_text('{"name": "Alice", "use_case": "drafting marketing emails", "language": "en"}'),  # extraction
    ]
    agent = Agent(settings, llm, session_manager, cost_tracker, tool_registry, memory_store=memory_store)

    await agent.handle_message("user-2", "test", "Hi!")
    await agent.handle_message("user-2", "test", "I'm Alice, I want help drafting marketing emails, in English.")

    session = await session_manager.get_or_create("user-2", "test")
    assert session.metadata[ONBOARDING_COMPLETE_KEY] == "true"
    assert session.metadata[PROFILE_NAME_KEY] == "Alice"
    assert "marketing" in session.metadata[PROFILE_USE_CASE_KEY]
    assert session.metadata[PROFILE_LANGUAGE_KEY] == "en"

    profiles = await memory_store.list_memories(user_id="user-2", category="profile")
    assert len(profiles) == 1
    assert "Alice" in profiles[0].content


@pytest.mark.asyncio
async def test_second_message_gibberish_still_closes_gate(
    settings, session_manager, cost_tracker, tool_registry, memory_store
):
    """Even an unparseable reply must close the gate — no re-prompting."""
    llm = AsyncMock()
    llm.complete.side_effect = [
        _llm_text("Hi!"),
        _llm_text("Got it."),
        _llm_text("not json"),  # extraction returns garbage
    ]
    agent = Agent(settings, llm, session_manager, cost_tracker, tool_registry, memory_store=memory_store)

    await agent.handle_message("user-3", "test", "hello")
    await agent.handle_message("user-3", "test", "asdf")

    session = await session_manager.get_or_create("user-3", "test")
    assert session.metadata[ONBOARDING_COMPLETE_KEY] == "true"
    assert PROFILE_NAME_KEY not in session.metadata

    # No profile memory written when nothing was extracted.
    profiles = await memory_store.list_memories(user_id="user-3", category="profile")
    assert profiles == []


@pytest.mark.asyncio
async def test_third_message_no_question_appended(settings, session_manager, cost_tracker, tool_registry, memory_store):
    llm = AsyncMock()
    llm.complete.side_effect = [
        _llm_text("Hi!"),
        _llm_text("Got it."),
        _llm_text('{"name": "Bob", "use_case": null, "language": "en"}'),
        _llm_text("Sure thing."),
    ]
    agent = Agent(settings, llm, session_manager, cost_tracker, tool_registry, memory_store=memory_store)

    await agent.handle_message("user-4", "test", "hi")
    await agent.handle_message("user-4", "test", "I'm Bob")
    result3 = await agent.handle_message("user-4", "test", "what's the weather?")

    assert ONBOARDING_QUESTION_EN not in result3.text


@pytest.mark.asyncio
async def test_preexisting_session_never_prompts(settings, mock_llm, session_manager, cost_tracker, tool_registry):
    """Users with prior messages never see the onboarding question."""
    from pincer.llm.base import LLMMessage, MessageRole

    session = await session_manager.get_or_create("user-old", "test")
    await session_manager.add_message(session, LLMMessage(role=MessageRole.USER, content="earlier message"))
    await session_manager.add_message(session, LLMMessage(role=MessageRole.ASSISTANT, content="earlier reply"))

    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    result = await agent.handle_message("user-old", "test", "hello again")

    assert ONBOARDING_QUESTION_EN not in result.text

    session = await session_manager.get_or_create("user-old", "test")
    assert ONBOARDING_PROMPT_SENT_KEY not in session.metadata


@pytest.mark.asyncio
async def test_extraction_failure_closes_gate(settings, session_manager, cost_tracker, tool_registry, memory_store):
    """If extraction call raises, the gate still closes."""
    llm = AsyncMock()
    llm.complete.side_effect = [
        _llm_text("Hi!"),
        _llm_text("Got it."),
        RuntimeError("LLM down"),
    ]
    agent = Agent(settings, llm, session_manager, cost_tracker, tool_registry, memory_store=memory_store)

    await agent.handle_message("user-5", "test", "hi")
    await agent.handle_message("user-5", "test", "I'm Carol")

    session = await session_manager.get_or_create("user-5", "test")
    assert session.metadata[ONBOARDING_COMPLETE_KEY] == "true"


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
