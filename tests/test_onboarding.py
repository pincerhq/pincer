"""Tests for first-session onboarding."""

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from pincer.core.agent import (
    _ONBOARDING_ANSWER_WINDOW_S,
    _ONBOARDING_EXTRACTION_MAX_TOKENS,
    _ONBOARDING_MAX_EMPTY_REPLIES,
    Agent,
    StreamEventType,
    _Onboarding,
)
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
from pincer.llm.base import LLMMessage, LLMResponse, MessageRole, StreamTurnEvent
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

    async def stream_turn(**_: object) -> AsyncIterator[StreamTurnEvent]:
        yield StreamTurnEvent(text=text)
        yield StreamTurnEvent(response=_llm_text(text))

    llm.stream_turn = stream_turn


def _streams_from_complete(llm: AsyncMock) -> None:
    """Web chat streams each step (slice 13): answer it as complete() is scripted to, and record it
    there, so complete()'s calls count every request on either path."""

    async def stream_turn(**kwargs: object) -> AsyncIterator[StreamTurnEvent]:
        response = await llm.complete(**kwargs)
        if response.content:
            yield StreamTurnEvent(text=response.content)
        yield StreamTurnEvent(response=response)

    llm.stream_turn = stream_turn


def _scripted_llm(*replies: str | Exception) -> AsyncMock:
    """An LLM whose requests answer in order, on either path."""
    llm = AsyncMock()
    llm.complete.side_effect = [r if isinstance(r, Exception) else _llm_text(r) for r in replies]
    _streams_from_complete(llm)
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
    memory: bool = True,
) -> str:
    """One turn on the blocking or the streaming path; the reply the user sees."""
    if path == "blocking":
        result = await agent.handle_message(
            user_id, channel, text, channel_name=channel_name, session_key=session_key, memory=memory
        )
        return result.text
    chunks = [
        c async for c in agent.handle_message_stream(user_id, channel, text, session_key=session_key, memory=memory)
    ]
    return [c.content for c in chunks if c.type == StreamEventType.DONE][-1]


async def _record(tmp_path: Path, user_id: str, channel: str) -> OnboardingRecord | None:
    """The stored onboarding row, read past every cache (the fixture's db is at head)."""
    return await OnboardingService(get_database_url(tmp_path / "test.db")).get(user_id, channel)


def _long_ago() -> float:
    """A session creation time past the answer window: no turn still asking wrote it."""
    return time.time() - _ONBOARDING_ANSWER_WINDOW_S - 3600


async def _store_history(
    session_manager,
    user_id: str,
    channel: str,
    *,
    session_key: str | None = None,
    reply: str = "earlier reply",
    created_at: float | None = None,
    **metadata,
):
    session = await session_manager.get_or_create(user_id, channel, session_key=session_key)
    if created_at is not None:
        session.created_at = created_at
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


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("Hello", "Hello\n\n" + ONBOARDING_QUESTION_EN),
        ("Hello\n", "Hello\n\n" + ONBOARDING_QUESTION_EN),
        ("Hello\n\n", "Hello\n\n" + ONBOARDING_QUESTION_EN),
        ("Hello\n\n\n", "Hello\n\n\n" + ONBOARDING_QUESTION_EN),
        ("Hello  ", "Hello  \n\n" + ONBOARDING_QUESTION_EN),
    ],
)
async def test_streamed_onboarding_tail_extends_the_text_the_client_received(
    reply, expected, settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """The question is separated from the reply by a blank line, and the chunks are an exact prefix of
    what is saved: DONE == join(TEXT) == the saved message, whatever whitespace the reply ends in."""
    _streams(mock_llm, reply)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    chunks = [c async for c in agent.handle_message_stream("user-tail", "test", "Hi!")]

    texts = [c.content for c in chunks if c.type == StreamEventType.TEXT]
    session = await session_manager.get_or_create("user-tail", "test")
    assert chunks[-1].content.endswith(ONBOARDING_QUESTION_EN)  # the precondition: the question was asked
    assert chunks[-1].content == "".join(texts) == session.messages[-1].content == expected


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


async def test_extraction_leaves_room_for_thinking(
    tmp_path, settings, session_manager, cost_tracker, tool_registry, memory_store
):
    """A model that thinks spends output tokens before the JSON (issue #345, Q17)."""
    llm = _scripted_llm(
        "Hello!",
        "Nice to meet you, Bob.",
        '{"answers_question": true, "name": "Bob", "use_case": "accounting", "language": "en"}',
    )
    agent = Agent(settings, llm, session_manager, cost_tracker, tool_registry, memory_store=memory_store)
    key = conversation_key("user-t", "web", "x")

    await _reply(agent, "blocking", "user-t", "web", "Hi!", session_key=key)
    await _reply(agent, "blocking", "user-t", "web", "I'm Bob, accountant", session_key=key)

    prompt_head = EXTRACTION_PROMPT_TEMPLATE.split("{reply}")[0]
    extractions = [
        call.kwargs for call in llm.complete.call_args_list if prompt_head in (call.kwargs["messages"][0].content or "")
    ]
    assert len(extractions) == 1, "the reply must have been sent for extraction"
    assert extractions[0]["max_tokens"] == 1024


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


async def _asked_a_day_ago(session_manager, user_id: str) -> None:
    """A question shown in the user's web session, asked more than a day ago."""
    await _store_history(session_manager, user_id, "web", reply=f"Hello!\n\n{ONBOARDING_QUESTION_EN}")
    past_window = time.time() - _ONBOARDING_ANSWER_WINDOW_S - 3600
    assert await session_manager.onboarding.claim_prompt(user_id, "web", f"web:{user_id}", now=past_window)


@_PATHS
async def test_an_unanswered_question_expires_after_a_day(
    path, tmp_path, settings, session_manager, cost_tracker, tool_registry, memory_store
):
    """More than a day after the question, a message that is no answer closes
    the gate as expired after one read, with no profile; nothing after it is read."""
    llm = _scripted_llm("Sure.", _TASK_REPLIES["new_prompt_response"], "Done.")
    agent = Agent(settings, llm, session_manager, cost_tracker, tool_registry, memory_store=memory_store)
    await _asked_a_day_ago(session_manager, "user-e")

    await _reply(agent, path, "user-e", "web", "Summarize this contract for me")

    assert llm.complete.call_count == 2
    record = await _record(tmp_path, "user-e", "web")
    assert record is not None
    assert (record.attempts, record.completed_at is not None, record.closed_by) == (0, True, "expired")
    assert await memory_store.list_memories(user_id="user-e", category="profile") == []

    await _reply(agent, path, "user-e", "web", "And the next one")

    assert llm.complete.call_count == 3
    assert ONBOARDING_FOLLOWUP_INSTRUCTION not in llm.complete.call_args_list[2].kwargs["system"]


@_PATHS
async def test_an_answer_after_a_day_is_read(
    path, tmp_path, settings, session_manager, cost_tracker, tool_registry, memory_store
):
    """Asked at 23:00, answered at 23:30 the next day: the answer is still
    taken, with the follow-up instruction, and the profile is written."""
    llm = _scripted_llm(
        "Nice to meet you, Ana.",
        '{"answers_question": true, "name": "Ana", "use_case": "travel planning", "language": null}',
    )
    agent = Agent(settings, llm, session_manager, cost_tracker, tool_registry, memory_store=memory_store)
    await _asked_a_day_ago(session_manager, "user-ana")

    await _reply(agent, path, "user-ana", "web", "I'm Ana, mostly travel planning")

    assert llm.complete.call_count == 2
    assert ONBOARDING_FOLLOWUP_INSTRUCTION in llm.complete.call_args_list[0].kwargs["system"]
    record = await _record(tmp_path, "user-ana", "web")
    assert record is not None
    assert (record.completed_at is not None, record.closed_by) == (True, "answer")
    profiles = await memory_store.list_memories(user_id="user-ana", category="profile")
    assert [("Ana" in p.content) for p in profiles] == [True]


@_PATHS
async def test_an_extraction_error_after_a_day_leaves_the_gate_open(
    path, tmp_path, settings, session_manager, cost_tracker, tool_registry, memory_store
):
    """A transient outage on the late read drops nothing: the gate stays open,
    and the next message gets the same last chance."""
    llm = _scripted_llm(
        "Sure.",
        RuntimeError("LLM down"),
        "Nice to meet you, Ana.",
        '{"answers_question": true, "name": "Ana", "use_case": null, "language": null}',
    )
    agent = Agent(settings, llm, session_manager, cost_tracker, tool_registry, memory_store=memory_store)
    await _asked_a_day_ago(session_manager, "user-ana2")

    await _reply(agent, path, "user-ana2", "web", "I'm Ana")

    record = await _record(tmp_path, "user-ana2", "web")
    assert record is not None
    assert (record.attempts, record.completed_at) == (1, None)

    await _reply(agent, path, "user-ana2", "web", "I'm Ana, again")

    record = await _record(tmp_path, "user-ana2", "web")
    assert record is not None and record.closed_by == "answer"
    assert len(await memory_store.list_memories(user_id="user-ana2", category="profile")) == 1


def _extracting_llm(extract) -> AsyncMock:
    """An LLM whose extraction calls go to ``extract(text)``; other calls reply "Ok."."""

    async def complete(**kwargs):
        if kwargs.get("max_tokens") == _ONBOARDING_EXTRACTION_MAX_TOKENS:
            return await extract(kwargs["messages"][0].content)
        return _llm_text("Ok.")

    llm = AsyncMock()
    llm.complete.side_effect = complete
    _streams(llm, "Ok.")
    return llm


@_PATHS
async def test_a_failing_extractor_after_a_day_stops_being_called(
    path, tmp_path, settings, session_manager, cost_tracker, tool_registry, memory_store
):
    """Extraction that keeps failing after the window is retried a bounded
    number of times, then the gate closes as expired and nothing more is read."""
    extractions = []

    async def extract(prompt: str) -> LLMResponse:
        extractions.append(prompt)
        raise RuntimeError("extractor down")

    agent = Agent(
        settings, _extracting_llm(extract), session_manager, cost_tracker, tool_registry, memory_store=memory_store
    )
    await _asked_a_day_ago(session_manager, "user-f")

    for n in range(_ONBOARDING_MAX_EMPTY_REPLIES + 2):
        await _reply(agent, path, "user-f", "web", f"Task {n}")

    assert len(extractions) == _ONBOARDING_MAX_EMPTY_REPLIES
    record = await _record(tmp_path, "user-f", "web")
    assert record is not None
    assert (record.completed_at is not None, record.closed_by) == (True, "expired")
    assert await memory_store.list_memories(user_id="user-f", category="profile") == []


@_PATHS
async def test_a_late_answer_takes_over_a_concurrent_late_expiry(
    path, tmp_path, settings, session_manager, cost_tracker, tool_registry, memory_store
):
    """After the window, a task and an answer are read at once. The task's
    expiry lands first; the answer still completes onboarding and writes the profile."""
    both_extracting = asyncio.Barrier(2)
    expired = asyncio.Event()
    onboarding = session_manager.onboarding
    complete = onboarding.complete

    async def expire_then_signal(*args, **kwargs):
        won = await complete(*args, **kwargs)
        expired.set()
        return won

    onboarding.complete = expire_then_signal

    async def extract(prompt: str) -> LLMResponse:
        await both_extracting.wait()
        if "I'm Ana" in prompt:
            await expired.wait()
            return _llm_text('{"answers_question": true, "name": "Ana", "use_case": null, "language": null}')
        return _llm_text(_TASK_REPLIES["new_prompt_response"])

    agent = Agent(
        settings, _extracting_llm(extract), session_manager, cost_tracker, tool_registry, memory_store=memory_store
    )
    await _asked_a_day_ago(session_manager, "user-r")

    await asyncio.wait_for(
        asyncio.gather(
            _reply(agent, path, "user-r", "web", "Summarize this contract for me"),
            _reply(agent, path, "user-r", "web", "I'm Ana", session_key=conversation_key("user-r", "web", "b")),
        ),
        timeout=10,
    )

    assert expired.is_set()
    record = await _record(tmp_path, "user-r", "web")
    assert record is not None and record.closed_by == "answer"
    profiles = await memory_store.list_memories(user_id="user-r", category="profile")
    assert [("Ana" in p.content) for p in profiles] == [True]


@_PATHS
async def test_a_turn_without_memory_is_never_asked(
    path, tmp_path, settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """A brand-new user's first request is a call summary: the summary is not
    followed by the question, and the user is still new for their first chat."""
    _streams(mock_llm, "The call went well.")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    key = conversation_key("user-s", "web", "summary-1")

    reply = await _reply(agent, path, "user-s", "web", "Summarize this call", session_key=key, memory=False)

    assert ONBOARDING_QUESTION_EN not in reply
    assert await _record(tmp_path, "user-s", "web") is None

    chat = conversation_key("user-s", "web", "chat-1")
    reply = await _reply(agent, path, "user-s", "web", "Hi!", session_key=chat)

    assert ONBOARDING_QUESTION_EN in reply


@_PATHS
async def test_a_turn_without_memory_is_not_read_for_an_answer(
    path, tmp_path, settings, session_manager, cost_tracker, tool_registry, memory_store
):
    """Asked, then a call summary arrives before the person answers: the
    summary prompt is not taken for their profile, and the question stays open."""
    llm = _scripted_llm("Hello!", "The call went well.")
    agent = Agent(settings, llm, session_manager, cost_tracker, tool_registry, memory_store=memory_store)

    await _reply(agent, path, "user-s", "web", "Hi!")
    await _reply(
        agent,
        path,
        "user-s",
        "web",
        "Summarize this call: I'm Sam, I run a bakery",
        session_key=conversation_key("user-s", "web", "summary-1"),
        memory=False,
    )

    assert llm.complete.call_count == 2  # no extraction call
    assert ONBOARDING_FOLLOWUP_INSTRUCTION not in llm.complete.call_args.kwargs["system"]
    record = await _record(tmp_path, "user-s", "web")
    assert record is not None
    assert (record.asked_session_id, record.completed_at) == ("web:user-s", None)
    assert await memory_store.list_memories(user_id="user-s", category="profile") == []


@_PATHS
@pytest.mark.parametrize("failing", ["save", "claim"])
async def test_a_failed_save_or_claim_sends_and_records_no_question(
    path, failing, tmp_path, settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """A turn whose reply or claim fails to save neither sends the question nor
    leaves it recorded. A claim whose insert committed before it raised is
    undone."""
    _streams(mock_llm, "Hello! I'm Pincer.")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    key = conversation_key("user-d", "web", "a")
    onboarding = session_manager.onboarding
    save, claim = session_manager.add_message, onboarding.claim_prompt

    async def add_message(session, message):
        if message.role == MessageRole.ASSISTANT:
            raise ConnectionResetError("store went away")
        await save(session, message)

    async def claim_prompt(*args, **kwargs):
        await claim(*args, **kwargs)
        raise ConnectionResetError("store went away")

    if failing == "save":
        session_manager.add_message = add_message
    else:
        onboarding.claim_prompt = claim_prompt
    chunks = []
    with pytest.raises(ConnectionResetError):
        if path == "blocking":
            await agent.handle_message("user-d", "web", "Hi!", session_key=key)
        else:
            async for chunk in agent.handle_message_stream("user-d", "web", "Hi!", session_key=key):
                chunks.append(chunk)
    session_manager.add_message, onboarding.claim_prompt = save, claim

    assert not any(ONBOARDING_QUESTION_EN in c.content for c in chunks)
    assert StreamEventType.DONE not in [c.type for c in chunks]
    assert await _record(tmp_path, "user-d", "web") is None
    session = await session_manager.get_or_create("user-d", "web", session_key=key)
    assert [m.role for m in session.messages] == [MessageRole.USER]
    assert agent._onboarding_reserved == set()


@_PATHS
async def test_a_turn_that_loses_the_claim_does_not_ask(
    path, tmp_path, settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """Another process claims between this turn's check and its claim: the
    question is left out, since the claim, not this turn, says who reads the
    answer."""
    _streams(mock_llm, "Hello! I'm Pincer.")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    other = conversation_key("user-r", "web", "a")
    onboarding = session_manager.onboarding
    claim = onboarding.claim_prompt

    async def claim_prompt(user_id, channel, session_id, *, now):
        await claim(user_id, channel, other, now=now)  # the other process, just first
        return await claim(user_id, channel, session_id, now=now)

    onboarding.claim_prompt = claim_prompt
    reply = await _reply(agent, path, "user-r", "web", "Hi!", session_key=conversation_key("user-r", "web", "b"))
    onboarding.claim_prompt = claim

    assert reply == "Hello! I'm Pincer."
    record = await _record(tmp_path, "user-r", "web")
    assert record is not None
    assert record.asked_session_id == other


async def _drop_stream_on_question(agent: Agent, user_id: str, key: str) -> None:
    """Run a streamed first turn and disconnect on the chunk carrying the question."""
    stream = agent.handle_message_stream(user_id, "web", "Hi!", session_key=key)
    async for chunk in stream:
        if ONBOARDING_QUESTION_EN in chunk.content:
            break
    else:
        pytest.fail("the question was never streamed")
    await stream.aclose()


async def test_a_disconnect_after_the_question_is_streamed_still_records_it(
    tmp_path, settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """The reply and the claim are saved before the question is streamed: a
    client that drops on it leaves the user asked."""
    _streams(mock_llm, "Hello! I'm Pincer.")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    key = conversation_key("user-d", "web", "a")

    await _drop_stream_on_question(agent, "user-d", key)

    record = await _record(tmp_path, "user-d", "web")
    assert record is not None
    assert (record.asked_session_id, record.completed_at) == (key, None)
    session = await session_manager.get_or_create("user-d", "web", session_key=key)
    assert session.messages[-1].role == MessageRole.ASSISTANT
    assert session.messages[-1].content.endswith(ONBOARDING_QUESTION_EN)
    assert agent._onboarding_reserved == set()


async def test_the_answer_after_a_disconnect_on_the_question_is_read(
    tmp_path, settings, session_manager, cost_tracker, tool_registry, memory_store
):
    """The message after a stream dropped on the question gets the follow-up
    instruction and completes onboarding."""
    llm = _scripted_llm(
        "Hello!",
        "Nice to meet you, Bob.",
        '{"answers_question": true, "name": "Bob", "use_case": "accounting", "language": "en"}',
    )
    agent = Agent(settings, llm, session_manager, cost_tracker, tool_registry, memory_store=memory_store)
    key = conversation_key("user-d", "web", "a")
    await _drop_stream_on_question(agent, "user-d", key)

    await _reply(agent, "stream", "user-d", "web", "I'm Bob, accountant", session_key=key)

    assert ONBOARDING_FOLLOWUP_INSTRUCTION in llm.complete.call_args_list[1].kwargs["system"]
    record = await _record(tmp_path, "user-d", "web")
    assert record is not None
    assert record.completed_at is not None
    profiles = await memory_store.list_memories(user_id="user-d", category="profile")
    assert [("Bob" in p.content) for p in profiles] == [True]


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
        if kwargs.get("max_tokens") == _ONBOARDING_EXTRACTION_MAX_TOKENS:  # the extraction call
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


async def test_releasing_a_claim_leaves_others_and_read_ones(session_manager):
    """`release_prompt` removes only the caller's own claim, and only while no
    reply has been read against it."""
    onboarding = session_manager.onboarding
    assert await onboarding.claim_prompt("user-x", "web", "web:a", now=1.0)
    assert not await onboarding.release_prompt("user-x", "web", "web:b")
    assert not await onboarding.record_empty_reply("user-x", "web", max_empty=3, now=2.0)
    assert not await onboarding.release_prompt("user-x", "web", "web:a")
    assert await onboarding.get("user-x", "web") is not None

    assert await onboarding.claim_prompt("user-y", "web", "web:a", now=1.0)
    assert await onboarding.release_prompt("user-y", "web", "web:a")
    assert await onboarding.get("user-y", "web") is None


async def test_inflight_answer_after_empty_closure_writes_profile(
    settings, session_manager, cost_tracker, tool_registry, memory_store
):
    answer = '{"answers_question": true, "name": "Bob"}'
    llm = _scripted_llm(answer)
    agent = Agent(settings, llm, session_manager, cost_tracker, tool_registry, memory_store=memory_store)
    onboarding = session_manager.onboarding
    assert await onboarding.claim_prompt("user-race", "web", "web:user-race", now=1.0)
    assert not await onboarding.record_empty_reply("user-race", "web", max_empty=2, now=2.0)
    assert await onboarding.record_empty_reply("user-race", "web", max_empty=2, now=3.0)

    await agent._ingest_onboarding_reply("I'm Bob", "user-race", "web", _Onboarding(prompt=False, ingest=True))

    record = await onboarding.get("user-race", "web")
    assert record is not None and record.attempts == 2
    profiles = await memory_store.list_memories(user_id="user-race", category="profile")
    assert len(profiles) == 1 and "Bob" in profiles[0].content


@_PATHS
async def test_existing_user_new_conversation_is_separate_and_not_prompted(
    path, tmp_path, settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """A web chat conversation is its own session, and a user with history in
    their main one is not asked who they are when they open "New chat"."""
    _streams_from_complete(mock_llm)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    await _store_history(session_manager, "user-t", "web", created_at=_long_ago())
    key = conversation_key("user-t", "web", "thread-1")

    reply = await _reply(agent, path, "user-t", "web", "Hi!", session_key=key)

    assert reply == "Hello! I'm Pincer."
    thread = await session_manager.get_or_create("user-t", "web", session_key=key)
    assert [m.content for m in thread.messages if m.role == MessageRole.USER] == ["Hi!"]
    main = await session_manager.get_or_create("user-t", "web")
    assert [m.content for m in main.messages if m.role == MessageRole.USER] == ["earlier message"]
    record = await _record(tmp_path, "user-t", "web")
    assert record is not None and record.completed_at is not None
    assert record.closed_by == "existing"
    assert mock_llm.complete.call_count == 1  # the turn only; nothing ingested


@_PATHS
async def test_history_only_in_conversations_is_not_new(
    path, tmp_path, settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """No main-chat history, but a stored conversation: not a new user."""
    _streams(mock_llm, "Hello! I'm Pincer.")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    await _store_history(
        session_manager, "user-c", "web", session_key=conversation_key("user-c", "web", "a"), created_at=_long_ago()
    )

    reply = await _reply(agent, path, "user-c", "web", "Hi!", session_key=conversation_key("user-c", "web", "b"))

    assert reply == "Hello! I'm Pincer."
    record = await _record(tmp_path, "user-c", "web")
    assert record is not None and record.completed_at is not None
    assert record.closed_by == "existing"


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
    _streams_from_complete(mock_llm)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    await _store_history(
        session_manager, "user-o", "web", **{ONBOARDING_PROMPT_SENT_KEY: "true", ONBOARDING_COMPLETE_KEY: "true"}
    )

    reply = await _reply(agent, path, "user-o", "web", "hello again")

    assert reply == "Hello! I'm Pincer."
    assert mock_llm.complete.call_count == 1
    assert ONBOARDING_FOLLOWUP_INSTRUCTION not in mock_llm.complete.call_args.kwargs["system"]
    record = await _record(tmp_path, "user-o", "web")
    assert record is not None and record.completed_at is not None
    assert record.closed_by == "legacy"


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


@pytest.mark.parametrize(
    "history",
    [{ONBOARDING_PROMPT_SENT_KEY: "true", ONBOARDING_COMPLETE_KEY: "true"}, {}],
    ids=["legacy", "existing"],
)
@_PATHS
async def test_a_known_user_s_second_turn_reads_no_onboarding_row(
    path, history, settings, mock_llm, session_manager, cost_tracker, tool_registry, monkeypatch
):
    """The first turn of a user with no row writes a completed one; the next
    hits the cache."""
    _streams(mock_llm, "Hello! I'm Pincer.")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    await _store_history(session_manager, "user-k", "web", created_at=_long_ago(), **history)
    await _reply(agent, path, "user-k", "web", "hello")
    stored = session_manager.onboarding.get
    reads = []

    async def get(user_id, channel):
        reads.append((user_id, channel))
        return await stored(user_id, channel)

    monkeypatch.setattr(session_manager.onboarding, "get", get)

    reply = await _reply(agent, path, "user-k", "web", "hello again")

    assert reply == "Hello! I'm Pincer."
    assert reads == []


def _gated_llm(entered: asyncio.Event, release: asyncio.Event) -> AsyncMock:
    """An LLM whose first call (blocking or streamed) waits for ``release``,
    after setting ``entered``; later calls answer at once."""
    llm = AsyncMock()
    calls = 0

    async def wait_if_first() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()

    async def complete(**_: object) -> LLMResponse:
        await wait_if_first()
        return _llm_text("Hello! I'm Pincer.")

    async def stream_turn(**_: object) -> AsyncIterator[StreamTurnEvent]:
        await wait_if_first()
        yield StreamTurnEvent(text="Hello! I'm Pincer.")
        yield StreamTurnEvent(response=_llm_text("Hello! I'm Pincer."))

    llm.complete.side_effect = complete
    llm.stream_turn = stream_turn
    return llm


@_PATHS
async def test_a_new_user_s_other_turn_does_not_close_the_gate_while_the_first_asks(
    path, tmp_path, settings, session_manager, cost_tracker, tool_registry
):
    """A brand-new user's first turn saves its message, then runs the model
    before it asks. Another turn meanwhile sees that history and is not new,
    but must not close the gate: the first turn's claim would lose and its
    question go unread."""
    entered, release = asyncio.Event(), asyncio.Event()
    agent = Agent(settings, _gated_llm(entered, release), session_manager, cost_tracker, tool_registry)
    first = asyncio.create_task(
        _reply(agent, path, "user-n", "web", "Hi!", session_key=conversation_key("user-n", "web", "a"))
    )
    await asyncio.wait_for(entered.wait(), timeout=5)

    other = await _reply(agent, path, "user-n", "web", "Hello?", session_key=conversation_key("user-n", "web", "b"))

    assert ONBOARDING_QUESTION_EN not in other
    assert await _record(tmp_path, "user-n", "web") is None
    release.set()
    reply = await asyncio.wait_for(first, timeout=5)
    assert ONBOARDING_QUESTION_EN in reply
    record = await _record(tmp_path, "user-n", "web")
    assert record is not None
    assert record.asked_at is not None and record.completed_at is None


@pytest.mark.parametrize("old", [False, True], ids=["recent_history", "old_history"])
@_PATHS
async def test_an_existing_user_is_closed_only_once_their_history_is_older_than_a_day(
    path, old, tmp_path, settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """History newer than the answer window might be an asking turn's own, so
    the user is left without a row (and still not asked) until it is older."""
    _streams(mock_llm, "Hello! I'm Pincer.")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    await _store_history(session_manager, "user-a", "web", created_at=_long_ago() if old else None)

    reply = await _reply(agent, path, "user-a", "web", "Hi!", session_key=conversation_key("user-a", "web", "b"))

    assert ONBOARDING_QUESTION_EN not in reply
    record = await _record(tmp_path, "user-a", "web")
    if old:
        assert record is not None and record.completed_at is not None
        assert record.closed_by == "existing"
    else:
        assert record is None


@_PATHS
async def test_a_failed_close_of_a_known_user_does_not_fail_the_turn(
    path, tmp_path, settings, mock_llm, session_manager, cost_tracker, tool_registry, monkeypatch
):
    _streams(mock_llm, "Hello! I'm Pincer.")
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    await _store_history(session_manager, "user-f", "web", created_at=_long_ago())
    complete_known = AsyncMock(side_effect=RuntimeError("db down"))
    monkeypatch.setattr(session_manager.onboarding, "complete_known", complete_known)

    reply = await _reply(agent, path, "user-f", "web", "hello again")

    assert reply == "Hello! I'm Pincer."
    assert ONBOARDING_QUESTION_EN not in reply
    complete_known.assert_awaited_once()
    assert await _record(tmp_path, "user-f", "web") is None


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
    await _store_history(session_manager, "user-old", "test", created_at=_long_ago())

    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    result = await agent.handle_message("user-old", "test", "hello again")

    assert ONBOARDING_QUESTION_EN not in result.text
    record = await _record(tmp_path, "user-old", "test")
    assert record is not None and record.completed_at is not None
    assert record.closed_by == "existing"


async def test_an_extraction_error_leaves_the_gate_untouched(
    tmp_path, settings, session_manager, cost_tracker, tool_registry, memory_store
):
    """A failed extraction call (an outage) is no reply at all: no
    attempt is counted, and the answer after it is still taken."""
    llm = _scripted_llm(
        "Hi!",
        "Got it.",
        RuntimeError("LLM down"),
        "Still here.",
        TimeoutError("LLM timed out"),
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


async def _rows(tmp_path: Path) -> int:
    from pincer.db.session import session_scope
    from pincer.repositories.costs import CostLogRepository

    async with session_scope(get_database_url(tmp_path / "test.db")) as session:
        return len(await CostLogRepository(session).list())


async def test_the_onboarding_extraction_is_recorded_like_any_request(  # type: ignore[no-untyped-def]
    tmp_path, settings, session_manager, cost_tracker, tool_registry, memory_store
) -> None:
    llm = _scripted_llm(
        "Hi!",
        "Nice to meet you, Carol.",
        '{"answers_question": true, "name": "Carol", "use_case": null, "language": null}',
    )
    agent = Agent(settings, llm, session_manager, cost_tracker, tool_registry, memory_store=memory_store)

    await agent.handle_message("user-6", "test", "hi")
    await agent.handle_message("user-6", "test", "I'm Carol")

    assert llm.complete.await_count == 3  # precondition: two replies and the extraction
    assert await _rows(tmp_path) == 3


async def test_with_the_budget_reached_the_onboarding_extraction_is_not_requested(  # type: ignore[no-untyped-def]
    tmp_path, settings, session_manager, tool_registry, memory_store
) -> None:
    """A turn the budget refused is no turn: no reply, no extraction, no attempt counted, the gate stays open."""
    from pincer.config.pricing import TokenUsage, load_price_table
    from pincer.llm.cost_tracker import CostTracker

    tracker = CostTracker(tmp_path / "test.db", daily_budget=1.0, prices=load_price_table())
    await tracker.initialize()
    llm = _scripted_llm("Hi!", "Nice to meet you, Carol.", '{"answers_question": true, "name": "Carol"}')
    agent = Agent(
        settings.model_copy(update={"daily_budget_usd": 1.0}),
        llm,
        session_manager,
        tracker,
        tool_registry,
        memory_store=memory_store,
    )
    try:
        await agent.handle_message("user-7", "test", "hi")
        await tracker.record("anthropic", "test-fixture-model", TokenUsage(input_tokens=1_000_000))  # $7 today
        await agent.handle_message("user-7", "test", "I'm Carol")
    finally:
        await tracker.close()

    assert llm.complete.await_count == 1  # the first reply only
    record = await _record(tmp_path, "user-7", "test")
    assert record is not None
    assert (record.attempts, record.completed_at) == (0, None)


@_PATHS
async def test_an_answer_turn_keeps_its_reply_when_its_costs_cannot_be_stored(  # type: ignore[no-untyped-def]
    path, tmp_path, settings, session_manager, tool_registry, memory_store
) -> None:
    """A cost-DB error after the reply was saved (or streamed) never replaces it (PR #366 review)."""
    from sqlalchemy import text

    from pincer.config.pricing import load_price_table
    from pincer.db.session import session_scope
    from pincer.llm.cost_tracker import CostTracker

    tracker = CostTracker(tmp_path / "test.db", daily_budget=0.0, prices=load_price_table())  # no reads
    await tracker.initialize()
    llm = _scripted_llm(
        "Hi!",
        "Nice to meet you, Carol.",
        '{"answers_question": true, "name": "Carol", "use_case": null, "language": null}',
    )
    agent = Agent(settings, llm, session_manager, tracker, tool_registry, memory_store=memory_store)
    try:
        await _reply(agent, path, "user-8", "test", "hi")
        async with session_scope(get_database_url(tmp_path / "test.db")) as session:
            await (await session.connection()).execute(text("DROP TABLE pincer_cost_logs"))
        reply = await _reply(agent, path, "user-8", "test", "I'm Carol")
    finally:
        await tracker.close()

    assert reply == "Nice to meet you, Carol."
    assert llm.complete.await_count == 3  # precondition: the extraction ran too
    record = await _record(tmp_path, "user-8", "test")
    assert record is not None and record.completed_at is not None


# test-model costs $0.00003 a request here (10 in at $1/MTok, 10 out at $2/MTok): a budget of
# $0.00005 admits the second turn, whose own first request then crosses it.
_CROSSED_BY_THE_ANSWER_TURN = 0.00005
_CAROL = '{"answers_question": true, "name": "Carol", "use_case": null, "language": null}'


async def _budgeted_agent(tmp_path, settings, session_manager, tool_registry, memory_store, llm):  # type: ignore[no-untyped-def]
    from pincer.config.pricing import load_price_table
    from pincer.llm.cost_tracker import CostTracker

    tracker = CostTracker(tmp_path / "test.db", daily_budget=_CROSSED_BY_THE_ANSWER_TURN, prices=load_price_table())
    await tracker.initialize()
    settings = settings.model_copy(update={"daily_budget_usd": _CROSSED_BY_THE_ANSWER_TURN})
    return Agent(settings, llm, session_manager, tracker, tool_registry, memory_store=memory_store), tracker


@_PATHS
async def test_a_turn_whose_own_reply_crosses_the_budget_still_reads_its_onboarding_answer(  # type: ignore[no-untyped-def]
    path, tmp_path, settings, session_manager, tool_registry, memory_store
) -> None:
    """The turn was admitted, so it runs to its end, extraction included, as a call in progress does."""
    llm = _scripted_llm("Hi!", "Nice to meet you, Carol.", _CAROL)
    agent, tracker = await _budgeted_agent(tmp_path, settings, session_manager, tool_registry, memory_store, llm)
    try:
        await _reply(agent, path, "user-9", "test", "hi")
        reply = await _reply(agent, path, "user-9", "test", "I'm Carol")
    finally:
        await tracker.close()

    assert reply == "Nice to meet you, Carol."
    assert llm.complete.await_count == 3
    assert await _rows(tmp_path) == 3  # the extraction is recorded too
    record = await _record(tmp_path, "user-9", "test")
    assert record is not None and record.completed_at is not None
    profiles = await memory_store.list_memories(user_id="user-9", category="profile")
    assert [("Carol" in p.content) for p in profiles] == [True]


async def test_a_web_chat_turn_stopped_by_the_budget_mid_way_still_reads_its_onboarding_answer(  # type: ignore[no-untyped-def]
    tmp_path, settings, session_manager, tool_registry, memory_store
) -> None:
    """Admitted, then stopped after its tool step: the budget is not checked again for the extraction."""
    from pincer.llm.base import ToolCall

    greet = LLMResponse(
        content="",
        tool_calls=[ToolCall(id="tc1", name="greet", arguments={"name": "Carol"})],
        model="test-model",
        input_tokens=10,
        output_tokens=10,
        stop_reason="tool_use",
    )
    llm = AsyncMock()
    llm.complete.side_effect = [_llm_text("Hi!"), greet, _llm_text(_CAROL)]
    _streams_from_complete(llm)
    agent, tracker = await _budgeted_agent(tmp_path, settings, session_manager, tool_registry, memory_store, llm)
    try:
        await _reply(agent, "stream", "user-10", "web", "hi")
        chunks = [c async for c in agent.handle_message_stream("user-10", "web", "I'm Carol, greet me")]
    finally:
        await tracker.close()

    assert chunks[-1].budget_reached  # precondition: stopped before the reply step
    assert llm.complete.await_count == 3  # hi, the tool step, the extraction
    record = await _record(tmp_path, "user-10", "web")
    assert record is not None and record.completed_at is not None


@_PATHS
async def test_a_turn_that_loses_the_budget_mid_way_sends_no_extraction(  # type: ignore[no-untyped-def]
    path, tmp_path, settings, session_manager, memory_store
) -> None:
    """Failed closed: no further paid request, the extraction included (PR #366 review)."""
    from sqlalchemy import text

    from pincer.config.pricing import load_price_table
    from pincer.db.session import session_scope
    from pincer.llm.base import ToolCall
    from pincer.llm.cost_tracker import CostTracker
    from pincer.tools.registry import ToolRegistry

    async def lookup(context: dict | None = None) -> str:
        async with session_scope(get_database_url(tmp_path / "test.db")) as session:
            await (await session.connection()).execute(text("DROP TABLE pincer_cost_logs"))
        return "found"

    registry = ToolRegistry()
    registry.register(
        name="lookup", description="Looks up", handler=lookup, parameters={"type": "object", "properties": {}}
    )
    look = LLMResponse(
        content="",
        tool_calls=[ToolCall(id="tc1", name="lookup", arguments={})],
        model="test-model",
        input_tokens=10,
        output_tokens=10,
        stop_reason="tool_use",
    )
    llm = AsyncMock()
    llm.complete.side_effect = [_llm_text("Hi!"), look, _llm_text(_CAROL)]
    _streams_from_complete(llm)
    tracker = CostTracker(tmp_path / "test.db", daily_budget=1.0, prices=load_price_table())
    await tracker.initialize()
    agent = Agent(
        settings.model_copy(update={"daily_budget_usd": 1.0}),
        llm,
        session_manager,
        tracker,
        registry,
        memory_store=memory_store,
    )
    try:
        await _reply(agent, path, "user-11", "test", "hi")
        await _reply(agent, path, "user-11", "test", "I'm Carol, look it up")
    finally:
        await tracker.close()

    assert llm.complete.await_count == 2  # hi, the tool step: no extraction
    record = await _record(tmp_path, "user-11", "test")
    assert record is not None
    assert (record.attempts, record.completed_at) == (0, None)
