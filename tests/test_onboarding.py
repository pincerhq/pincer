"""Tests for first-session onboarding."""

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from pincer.core.agent import Agent, StreamEventType
from pincer.core.onboarding import (
    EXTRACTION_PROMPT_TEMPLATE,
    ONBOARDING_FOLLOWUP_INSTRUCTION,
    ONBOARDING_QUESTION_EN,
)
from pincer.core.session import (
    ONBOARDING_COMPLETE_KEY,
    ONBOARDING_PROMPT_SENT_KEY,
    conversation_key,
)
from pincer.db.engine import get_database_url
from pincer.exceptions import BudgetExceededError
from pincer.llm.base import LLMMessage, LLMResponse, MessageRole
from pincer.memory.store import MemoryStore
from pincer.repositories.onboarding import OnboardingRepository
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


async def _store_history(
    session_manager,
    user_id: str,
    channel: str,
    *,
    session_key: str | None = None,
    reply: str = "earlier reply",
    **metadata,
):
    session = await session_manager.get_or_create(user_id, channel, session_key=session_key)
    session.metadata.update(metadata)
    await session_manager.add_message(session, LLMMessage(role=MessageRole.USER, content="earlier message"))
    await session_manager.add_message(session, LLMMessage(role=MessageRole.ASSISTANT, content=reply))


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
        "Hello!",
        "Nice to meet you, Bob.",
        '{"answers_question": true, "name": "Bob", "use_case": "accounting", "language": "en"}',
        "Sure.",
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


_EMPTY = '{"name": null, "use_case": null, "language": null}'


@_PATHS
async def test_answer_in_a_new_conversation_is_captured(
    path, tmp_path, settings, session_manager, cost_tracker, tool_registry, memory_store
):
    """Asked in the main chat, answered in a new one: captured there, and the
    next main-chat task is not taken for an answer."""
    llm = _scripted_llm(
        "Hello!",
        "Nice to meet you, Bob.",
        '{"answers_question": true, "name": "Bob", "use_case": "accounting"}',
        "Here is your summary.",
    )
    agent = Agent(settings, llm, session_manager, cost_tracker, tool_registry, memory_store=memory_store)

    await _reply(agent, path, "user-b", "web", "Hi!")
    await _reply(
        agent, path, "user-b", "web", "I'm Bob, accountant", session_key=conversation_key("user-b", "web", "x")
    )

    # The new chat never showed the question, so no "you just asked" instruction.
    assert ONBOARDING_FOLLOWUP_INSTRUCTION not in llm.complete.call_args_list[1].kwargs["system"]
    record = await _record(tmp_path, "user-b", "web")
    assert record is not None
    assert record.completed_at is not None
    profiles = await memory_store.list_memories(user_id="user-b", category="profile")
    assert [("Bob" in p.content) for p in profiles] == [True]

    await _reply(agent, path, "user-b", "web", "summarize my emails")

    assert llm.complete.call_count == 4
    assert ONBOARDING_FOLLOWUP_INSTRUCTION not in llm.complete.call_args_list[3].kwargs["system"]


@_PATHS
async def test_a_reply_with_no_profile_leaves_the_gate_open_once(
    path, tmp_path, settings, session_manager, cost_tracker, tool_registry, memory_store
):
    """A task sent after the question holds no answer: it is not saved as a
    profile, and the message after it is still read for one."""
    llm = _scripted_llm(
        "Hello!", "Here is your summary.", _EMPTY, "Nice to meet you, Bob.", '{"answers_question": true, "name": "Bob"}'
    )
    agent = Agent(settings, llm, session_manager, cost_tracker, tool_registry, memory_store=memory_store)
    other = conversation_key("user-g", "web", "b")

    await _reply(agent, path, "user-g", "web", "Hi!")
    await _reply(agent, path, "user-g", "web", "summarize my emails", session_key=other)

    record = await _record(tmp_path, "user-g", "web")
    assert record is not None
    assert (record.attempts, record.completed_at) == (1, None)
    assert await memory_store.list_memories(user_id="user-g", category="profile") == []

    await _reply(agent, path, "user-g", "web", "I'm Bob", session_key=other)

    assert llm.complete.call_count == 5
    record = await _record(tmp_path, "user-g", "web")
    assert record is not None
    assert record.completed_at is not None
    profiles = await memory_store.list_memories(user_id="user-g", category="profile")
    assert [("Bob" in p.content) for p in profiles] == [True]


@_PATHS
async def test_two_empty_replies_close_the_gate(
    path, tmp_path, settings, session_manager, cost_tracker, tool_registry, memory_store
):
    """Asked in A, the user only uses B: the gate closes after one extra
    message, with no profile, and nothing is read after that."""
    llm = _scripted_llm("Hello!", "Summary.", _EMPTY, "Calendar.", _EMPTY, "Weather.")
    agent = Agent(settings, llm, session_manager, cost_tracker, tool_registry, memory_store=memory_store)
    b = conversation_key("user-q", "web", "b")

    await _reply(agent, path, "user-q", "web", "Hi!", session_key=conversation_key("user-q", "web", "a"))
    await _reply(agent, path, "user-q", "web", "summarize my emails", session_key=b)
    await _reply(agent, path, "user-q", "web", "and my calendar", session_key=b)
    await _reply(agent, path, "user-q", "web", "and the weather", session_key=b)

    assert llm.complete.call_count == 6
    record = await _record(tmp_path, "user-q", "web")
    assert record is not None
    assert (record.attempts, record.completed_at is not None) == (2, True)
    assert await memory_store.list_memories(user_id="user-q", category="profile") == []


@_PATHS
async def test_an_unanswered_question_expires_after_a_day(
    path, tmp_path, settings, session_manager, cost_tracker, tool_registry, memory_store
):
    """A question left unanswered for more than a day is closed by the next
    message, which is not read for an answer."""
    llm = _scripted_llm("Sure.")
    agent = Agent(settings, llm, session_manager, cost_tracker, tool_registry, memory_store=memory_store)
    await _store_history(session_manager, "user-e", "web")
    a_day_ago = time.time() - 25 * 60 * 60
    assert await session_manager.onboarding.claim_prompt("user-e", "web", "web:user-e", now=a_day_ago)

    await _reply(agent, path, "user-e", "web", "I'm Eve")

    assert llm.complete.call_count == 1
    assert ONBOARDING_FOLLOWUP_INSTRUCTION not in llm.complete.call_args.kwargs["system"]
    record = await _record(tmp_path, "user-e", "web")
    assert record is not None
    assert record.completed_at is not None
    assert await memory_store.list_memories(user_id="user-e", category="profile") == []


@_PATHS
async def test_a_disconnect_before_the_reply_is_saved_leaves_no_claim(
    path, tmp_path, settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """The question counts as asked only once the reply carrying it is saved."""
    _streams(mock_llm, "Hello! I'm Pincer.")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    key = conversation_key("user-d", "web", "a")

    if path == "blocking":
        save = session_manager.add_message

        async def add_message(session, message):
            if message.role == MessageRole.ASSISTANT:
                raise ConnectionResetError("client went away")
            await save(session, message)

        session_manager.add_message = add_message
        with pytest.raises(ConnectionResetError):
            await agent.handle_message("user-d", "web", "Hi!", session_key=key)
        session_manager.add_message = save
    else:
        stream = agent.handle_message_stream("user-d", "web", "Hi!", session_key=key)
        async for chunk in stream:
            if ONBOARDING_QUESTION_EN in chunk.content:
                break
        else:
            pytest.fail("the question was never streamed")
        await stream.aclose()

    assert await _record(tmp_path, "user-d", "web") is None


# What an extraction prompt elicits from a model for a task message. These are
# stubs written from the prompts, not recorded model output.
_TASK_REPLIES = {
    # 06e136b's prompt: infer the language from the text, and take any purpose as the use case.
    "old_prompt_response": '{"name": null, "use_case": "summarizing contracts", "language": "en"}',
    # This prompt: the model is asked whether the message answers the question at all.
    "new_prompt_response": (
        '{"answers_question": false, "name": null, "use_case": "summarizing contracts", "language": "en"}'
    ),
}


@pytest.mark.parametrize("extraction", list(_TASK_REPLIES))
@_PATHS
async def test_a_task_message_is_not_an_answer(
    path, extraction, tmp_path, settings, session_manager, cost_tracker, tool_registry, memory_store
):
    """Asked in A, a task sent in B is not an answer, even when the model fills
    in a language and a use case for it: no profile, one attempt counted."""
    llm = _scripted_llm("Hello!", "Here is the summary.", _TASK_REPLIES[extraction])
    agent = Agent(settings, llm, session_manager, cost_tracker, tool_registry, memory_store=memory_store)

    await _reply(agent, path, "user-s", "web", "Hi!", session_key=conversation_key("user-s", "web", "a"))
    await _reply(
        agent,
        path,
        "user-s",
        "web",
        "Summarize this contract for me",
        session_key=conversation_key("user-s", "web", "b"),
    )

    record = await _record(tmp_path, "user-s", "web")
    assert record is not None
    assert (record.attempts, record.completed_at) == (1, None)
    assert await memory_store.list_memories(user_id="user-s", category="profile") == []


@_PATHS
async def test_a_real_answer_in_another_session_completes(
    path, tmp_path, settings, session_manager, cost_tracker, tool_registry, memory_store
):
    """The answer the question asks for, given in another session (a stub of
    what the prompt elicits for it), completes onboarding with that profile."""
    llm = _scripted_llm(
        "Hello!",
        "Nice to meet you, Bob.",
        '{"answers_question": true, "name": "Bob", "use_case": "accounting", "language": "de"}',
    )
    agent = Agent(settings, llm, session_manager, cost_tracker, tool_registry, memory_store=memory_store)

    await _reply(agent, path, "user-a", "web", "Hi!", session_key=conversation_key("user-a", "web", "a"))
    await _reply(
        agent,
        path,
        "user-a",
        "web",
        "I'm Bob, I do accounting, German please",
        session_key=conversation_key("user-a", "web", "b"),
    )

    record = await _record(tmp_path, "user-a", "web")
    assert record is not None
    assert record.completed_at is not None
    profiles = await memory_store.list_memories(user_id="user-a", category="profile")
    assert len(profiles) == 1
    assert "Bob" in profiles[0].content
    assert "de" in profiles[0].content


async def test_an_answer_with_no_fields_is_an_empty_reply(
    tmp_path, settings, session_manager, cost_tracker, tool_registry, memory_store
):
    llm = _scripted_llm("Hello!", "Ok.", '{"answers_question": true, "name": null, "use_case": null, "language": null}')
    agent = Agent(settings, llm, session_manager, cost_tracker, tool_registry, memory_store=memory_store)

    await agent.handle_message("user-n", "web", "Hi!")
    await agent.handle_message("user-n", "web", "hm")

    record = await _record(tmp_path, "user-n", "web")
    assert record is not None
    assert (record.attempts, record.completed_at) == (1, None)


def test_the_extraction_prompt_asks_for_a_judgement_and_never_infers_language():
    assert '"answers_question"' in EXTRACTION_PROMPT_TEMPLATE
    assert "inferable from the language they wrote in" not in EXTRACTION_PROMPT_TEMPLATE


@_PATHS
async def test_concurrent_answers_write_one_profile(
    path, tmp_path, settings, session_manager, cost_tracker, tool_registry, memory_store
):
    """Two sessions read an answer at once: only the turn that completes
    onboarding writes the profile."""
    both_extracting = asyncio.Barrier(2)
    answer = '{"answers_question": true, "name": "Bob", "use_case": "accounting", "language": "en"}'

    async def complete(**kwargs):
        if kwargs.get("max_tokens") == 200:  # the extraction call
            await both_extracting.wait()
            return _llm_text(answer)
        return _llm_text("Nice to meet you.")

    llm = AsyncMock()
    llm.complete.side_effect = complete
    _streams(llm, "Nice to meet you.")
    agent = Agent(settings, llm, session_manager, cost_tracker, tool_registry, memory_store=memory_store)
    await _store_history(session_manager, "user-c2", "web", reply=f"Hello!\n\n{ONBOARDING_QUESTION_EN}")
    assert await session_manager.onboarding.claim_prompt("user-c2", "web", "web:user-c2", now=time.time())
    writes = []
    add_profile = memory_store.add_profile

    async def counting_add_profile(**kwargs):
        writes.append(kwargs["user_id"])
        return await add_profile(**kwargs)

    memory_store.add_profile = counting_add_profile

    await asyncio.wait_for(
        asyncio.gather(
            _reply(agent, path, "user-c2", "web", "I'm Bob"),
            _reply(
                agent, path, "user-c2", "web", "Bob, accounting", session_key=conversation_key("user-c2", "web", "b")
            ),
        ),
        timeout=10,
    )

    assert writes == ["user-c2"]
    assert len(await memory_store.list_memories(user_id="user-c2", category="profile")) == 1


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
    llm = _scripted_llm("Nice to meet you, Dana.", '{"answers_question": true, "name": "Dana"}')
    agent = Agent(settings, llm, session_manager, cost_tracker, tool_registry)
    await _store_history(
        session_manager,
        "user-l",
        "web",
        reply=f"Hello!\n\n{ONBOARDING_QUESTION_EN}",  # what the old code appended, with the flag
        **{ONBOARDING_PROMPT_SENT_KEY: "true"},
    )

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
async def test_a_turn_that_loses_to_a_turn_in_flight_does_not_ask(
    path, tmp_path, settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """Two first messages at once: while one is asking, the other does not."""
    _streams(mock_llm, "Hello! I'm Pincer.")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    agent._onboarding_reserved = {("user-r", "web")}  # the other turn, between its question and its save

    reply = await _reply(agent, path, "user-r", "web", "Hi!", session_key=conversation_key("user-r", "web", "b"))

    assert reply == "Hello! I'm Pincer."
    assert await _record(tmp_path, "user-r", "web") is None


@_PATHS
async def test_a_turn_that_loses_to_a_finished_turn_does_not_ask(
    path, tmp_path, settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """Decided before the other turn's claim landed: it sees the claim before
    it asks, and does not."""
    _streams(mock_llm, "Hello! I'm Pincer.")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    other = conversation_key("user-r", "web", "a")
    assert await session_manager.onboarding.claim_prompt("user-r", "web", other, now=1.0)
    stored = session_manager.onboarding.get
    calls = []

    async def get(user_id, channel):
        calls.append(user_id)
        return None if len(calls) == 1 else await stored(user_id, channel)  # decided before the claim

    session_manager.onboarding.get = get

    reply = await _reply(agent, path, "user-r", "web", "Hi!", session_key=conversation_key("user-r", "web", "b"))

    session_manager.onboarding.get = stored
    assert reply == "Hello! I'm Pincer."
    record = await _record(tmp_path, "user-r", "web")
    assert record is not None
    assert record.asked_session_id == other


@_PATHS
async def test_an_onboarded_user_s_turn_reads_no_onboarding_row(
    path, settings, mock_llm, session_manager, cost_tracker, tool_registry, monkeypatch
):
    _streams(mock_llm, "Hello! I'm Pincer.")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    await session_manager.onboarding.complete("user-k", "web", now=1.0)
    await _store_history(session_manager, "user-k", "web")
    reads = []
    real_get = OnboardingRepository.get

    async def counting_get(self, pk):
        reads.append(pk)
        return await real_get(self, pk)

    monkeypatch.setattr(OnboardingRepository, "get", counting_get)

    await _reply(agent, path, "user-k", "web", "hello", session_key=conversation_key("user-k", "web", "z"))

    assert reads == []


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
        '{"answers_question": true, "name": "Alice", "use_case": "drafting marketing emails", "language": "en"}',
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


async def test_gibberish_replies_close_the_gate_on_the_second(
    tmp_path, settings, session_manager, cost_tracker, tool_registry, memory_store
):
    """An unparseable reply is an empty one: the gate stays open once, then closes."""
    llm = _scripted_llm("Hi!", "Got it.", "not json", "Ok.", "still not json")
    agent = Agent(settings, llm, session_manager, cost_tracker, tool_registry, memory_store=memory_store)

    await agent.handle_message("user-3", "test", "hello")
    await agent.handle_message("user-3", "test", "asdf")

    record = await _record(tmp_path, "user-3", "test")
    assert record is not None
    assert (record.attempts, record.completed_at) == (1, None)

    await agent.handle_message("user-3", "test", "qwer")

    record = await _record(tmp_path, "user-3", "test")
    assert record is not None
    assert record.completed_at is not None
    # No profile memory written when nothing was extracted.
    assert await memory_store.list_memories(user_id="user-3", category="profile") == []


async def test_third_message_no_question_appended(settings, session_manager, cost_tracker, tool_registry, memory_store):
    llm = _scripted_llm(
        "Hi!", "Got it.", '{"answers_question": true, "name": "Bob", "use_case": null, "language": "en"}', "Sure thing."
    )
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


async def test_an_extraction_error_leaves_the_gate_untouched(
    tmp_path, settings, session_manager, cost_tracker, tool_registry, memory_store
):
    """A failed extraction call (an outage, the budget) is no reply at all: no
    attempt is counted, and the answer after it is still taken."""
    llm = _scripted_llm(
        "Hi!",
        "Got it.",
        RuntimeError("LLM down"),
        "Still here.",
        BudgetExceededError(spent=5.0, limit=5.0),
        "Nice to meet you, Carol.",
        '{"answers_question": true, "name": "Carol", "use_case": null, "language": null}',
    )
    agent = Agent(settings, llm, session_manager, cost_tracker, tool_registry, memory_store=memory_store)

    await agent.handle_message("user-5", "test", "hi")
    await agent.handle_message("user-5", "test", "I'm Carol")
    await agent.handle_message("user-5", "test", "I'm Carol, again")

    record = await _record(tmp_path, "user-5", "test")
    assert record is not None
    assert (record.attempts, record.completed_at) == (0, None)

    await agent.handle_message("user-5", "test", "Carol. My name is Carol.")

    record = await _record(tmp_path, "user-5", "test")
    assert record is not None
    assert record.completed_at is not None
    profiles = await memory_store.list_memories(user_id="user-5", category="profile")
    assert [("Carol" in p.content) for p in profiles] == [True]


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
