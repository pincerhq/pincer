"""A history the endpoint rejects as too long is cut and retried, not left to fail every turn (PR #379 review)."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import pytest
from anthropic_sse import Reject, ScriptedAnthropic, text
from test_photos_stay import jpeg

from pincer.core.agent import Agent, StreamEventType
from pincer.exceptions import LLMError
from pincer.llm.base import LLMMessage, LLMResponse, MessageRole
from pincer.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from pincer.core.session import Session, SessionManager

_TOO_LONG = "prompt is too long: 250000 tokens > 200000 maximum"
_OK = ([text("Done.")], "end_turn")
_PAIRS = 20


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry()


async def _long_session(
    sessions: SessionManager, key: str | None = None, *, photo: bool = False, pairs: int = _PAIRS
) -> Session:
    """A session of a summary and `pairs` user/assistant pairs of 400 characters each; the first user
    message shows a photo when asked."""
    from pincer.llm.base import ImageContent

    session = await sessions.get_or_create("user1", "test", session_key=key)
    await sessions.add_message(
        session, LLMMessage(role=MessageRole.SYSTEM, content="[Previous conversation summary]\nSUM")
    )
    for pair in range(pairs):
        images = [ImageContent.from_bytes(jpeg(64, 64), "image/jpeg")] if photo and pair == 0 else []
        await sessions.add_message(
            session, LLMMessage(role=MessageRole.USER, content=f"question {pair} " + "q" * 380, images=images)
        )
        await sessions.add_message(
            session, LLMMessage(role=MessageRole.ASSISTANT, content=f"answer {pair} " + "a" * 380)
        )
    return session


def _sent(request: dict[str, Any]) -> int:
    return len(request["messages"])


async def _stored_photos(sessions: SessionManager, key: str) -> int:
    row = await sessions._store.load(key, user_id="user1", channel="test", legacy_fallback=False)
    assert row is not None
    return len(row["images"])


async def test_a_chat_turn_on_a_history_the_endpoint_rejects_cuts_the_oldest_half_and_goes_through(
    settings, session_manager, cost_tracker, registry, caplog
):
    caplog.set_level(logging.WARNING, logger="pincer.core.agent")
    session = await _long_session(session_manager)
    summary = session.messages[0]
    before = len(session.messages)
    async with ScriptedAnthropic(settings, [Reject(400, _TOO_LONG), _OK]) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry)
        reply = await agent.handle_message("user1", "test", "hello", memory=False)

    assert reply.text == "Done."
    assert len(llm.requests) == 2
    assert _sent(llm.requests[1]) < _sent(llm.requests[0]) * 0.6  # about half of the history went
    assert session.messages[0] is summary  # the summary of the talk before stays
    assert session.messages[1].role is MessageRole.USER  # a turn boundary
    assert session.messages[-2].content == "hello"
    kept = len(session.messages) - 2  # without the new user message and the reply
    assert before * 0.4 < kept < before * 0.6
    row = await session_manager._store.load("test:user1", user_id="user1", channel="test", legacy_fallback=False)
    assert row is not None
    assert len(json.loads(row["messages"])) == len(session.messages)  # saved
    dropped = before + 1 - (len(session.messages) - 1)
    assert [r.getMessage() for r in caplog.records if "too long" in r.getMessage()] == [
        f"Session test:user1: the endpoint rejected the history as too long; dropped the oldest {dropped} messages"
    ]


async def test_the_photo_that_made_the_history_too_big_goes_with_its_turn(
    settings, session_manager, cost_tracker, registry
):
    session = await _long_session(session_manager, photo=True)
    assert await _stored_photos(session_manager, "test:user1") == 1  # precondition: the photo is in the store
    async with ScriptedAnthropic(
        settings, [Reject(413, "Request exceeds the maximum allowed number of bytes."), _OK]
    ) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry)
        await agent.handle_message("user1", "test", "hello", memory=False)

    assert len(llm.requests) == 2
    assert not any(m.images for m in session.messages)
    assert session.messages[1].content.startswith("question 1 ")  # the photo's turn was the one that went
    assert await _stored_photos(session_manager, "test:user1") == 0


async def test_a_streamed_turn_recovers_the_same_way(settings, session_manager, cost_tracker, registry):
    session = await _long_session(session_manager)
    async with ScriptedAnthropic(settings, [Reject(400, _TOO_LONG), _OK]) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry)
        chunks = [c async for c in agent.handle_message_stream("user1", "test", "hello", memory=False)]

    assert [c.content for c in chunks if c.type == StreamEventType.TEXT] == ["Done."]
    assert len(llm.requests) == 2
    assert _sent(llm.requests[1]) < _sent(llm.requests[0]) * 0.6
    assert len(session.messages) < _PAIRS * 2


async def test_a_voice_utterance_recovers_the_same_way(settings, session_manager, cost_tracker, registry):
    await session_manager.onboarding.complete("user1", "voice", now=1.0)
    session = await _long_session(session_manager, key="voice:CA_long")
    async with ScriptedAnthropic(settings, [Reject(400, _TOO_LONG), _OK]) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry)
        chunks = [c async for c in agent.stream_voice_turn("user1", "voice", "hello", session_key="voice:CA_long")]

    assert [c.content for c in chunks if c.type == StreamEventType.TEXT] == ["Done."]
    assert len(llm.requests) == 2
    assert _sent(llm.requests[1]) < _sent(llm.requests[0]) * 0.6
    assert len(session.messages) < _PAIRS * 2


async def test_a_history_far_over_the_window_is_halved_again_until_it_fits(
    settings, session_manager, cost_tracker, registry
):
    await _long_session(session_manager)
    async with ScriptedAnthropic(settings, [Reject(400, _TOO_LONG)] * 3 + [_OK]) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry)
        await agent.handle_message("user1", "test", "hello", memory=False)

    sizes = [_sent(r) for r in llm.requests]
    assert len(sizes) == 4
    assert sizes[0] > sizes[1] > sizes[2] > sizes[3]
    assert sizes[3] < sizes[0] / 5  # about an eighth: three halvings


async def test_it_gives_up_after_four_cuts_and_raises_the_endpoints_error(
    settings, session_manager, cost_tracker, registry
):
    session = await _long_session(session_manager, pairs=100)  # 201 messages: a fifth halving would still cut
    async with ScriptedAnthropic(settings, [Reject(400, _TOO_LONG)] * 6) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry)
        with pytest.raises(LLMError, match="prompt is too long"):
            await agent.handle_message("user1", "test", "hello", memory=False)

    assert len(llm.requests) == 5  # the first and four retries
    assert 10 < len(session.messages) < 25  # 201 halved four times, plus the question; not a clear
    row = await session_manager._store.load("test:user1", user_id="user1", channel="test", legacy_fallback=False)
    assert row is not None
    assert len(json.loads(row["messages"])) == len(session.messages)  # every cut was saved, the turn failed anyway


async def test_one_giant_turn_cannot_be_cut_and_the_error_is_raised(settings, session_manager, cost_tracker, registry):
    session = await session_manager.get_or_create("user1", "test")
    async with ScriptedAnthropic(settings, [Reject(400, _TOO_LONG)]) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry)
        with pytest.raises(LLMError, match="prompt is too long"):
            await agent.handle_message("user1", "test", "x" * 50_000, memory=False)

    assert len(llm.requests) == 1
    assert [m.role for m in session.messages] == [MessageRole.USER]  # nothing to drop but the question itself


@pytest.mark.parametrize("message", ["scripted failure", "tool name is too long", "Your credit balance is too low"])
async def test_any_other_rejection_cuts_nothing(settings, session_manager, cost_tracker, registry, message):
    session = await _long_session(session_manager)
    before = len(session.messages)
    async with ScriptedAnthropic(settings, [Reject(400, message)] * 4) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry)
        with pytest.raises(LLMError):
            await agent.handle_message("user1", "test", "hello", memory=False)

    assert len(session.messages) == before + 1  # nothing went: only the question was added
    assert len(llm.requests) == 1


async def test_the_recovery_does_not_depend_on_the_wire(settings, session_manager, cost_tracker, registry, mock_llm):
    """Every provider's wording of the error, through a provider that is not Anthropic's."""
    session = await _long_session(session_manager)
    answer = LLMResponse(content="ok", model="m", input_tokens=10, output_tokens=5, stop_reason="end_turn")
    mock_llm.complete.side_effect = [
        LLMError(
            "openai API error: 400 This model's maximum context length is 32768 tokens. "
            "However, your messages resulted in 40000 tokens."
        ),
        answer,
    ]
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, registry)

    reply = await agent.handle_message("user1", "test", "hello", memory=False)

    assert reply.text == "ok"
    first, second = (len(call.kwargs["messages"]) for call in mock_llm.complete.await_args_list)
    assert second < first * 0.6
    assert len(session.messages) < _PAIRS * 2


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Anthropic API error: 400 prompt is too long: 250000 tokens > 200000 maximum", True),
        ("openai API error: 400 This model's maximum context length is 128000 tokens.", True),
        ("openai API error: 400 context_length_exceeded", True),
        ("azure API error: 400 Your input exceeds the context window of this model.", True),
        ("groq API error: 400 Please reduce the length of the messages or completion.", True),
        ("Anthropic API error: 413 Request exceeds the maximum allowed number of bytes.", True),
        ("Anthropic API error: 400 messages.1: `tool_use` ids were found without `tool_result` blocks", False),
        ("Anthropic API error: 429 rate limited", False),
        ("openai API error: 400 tool name is too long", False),
        ("Anthropic connection error: timed out", False),
    ],
)
def test_which_errors_say_the_history_is_too_long(message, expected):
    from pincer.core.agent import _is_context_length_error

    assert _is_context_length_error(LLMError(message)) is expected
