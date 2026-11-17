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
from pincer.llm.base import LLMMessage, LLMResponse, MessageRole, StreamTurnEvent, ToolCall
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


async def test_it_gives_up_after_four_cuts_and_raises_the_endpoints_error_with_the_history_as_it_was(
    settings, session_manager, cost_tracker, registry
):
    """The cuts are saved only once a request goes through: a wrong guess (an endpoint that rejects for another
    reason, in words that look like a window error) must not cost the history."""
    session = await _long_session(session_manager, pairs=100)  # 201 messages: a fifth halving would still cut
    before = list(session.messages)
    async with ScriptedAnthropic(settings, [Reject(400, _TOO_LONG)] * 6) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry)
        with pytest.raises(LLMError, match="prompt is too long"):
            await agent.handle_message("user1", "test", "hello", memory=False)

    assert len(llm.requests) == 5  # the first and four retries
    assert session.messages[: len(before)] == before  # nothing was cut for good...
    assert [m.content for m in session.messages[len(before) :]] == ["hello"]  # ...only the question was added
    row = await session_manager._store.load("test:user1", user_id="user1", channel="test", legacy_fallback=False)
    assert row is not None
    assert len(json.loads(row["messages"])) == len(session.messages)


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
        (
            "grok API error: 400 This model's maximum prompt length is 131072 but the request contains 150000 tokens.",
            True,
        ),
        (
            "gemini API error: 400 The input token count (1196265) exceeds the maximum number of tokens allowed "
            "(1048575).",
            True,
        ),
        (
            "openai API error: 400 This model's maximum context length is 32768 tokens. However, you requested "
            "40000 tokens (39000 in the messages, 1000 in the completion). Please reduce the length of the messages "
            "or completion.",
            True,
        ),
        (  # the usual case: the history still fits on its own, history and completion together do not
            "openai API error: 400 This model's maximum context length is 32768 tokens. However, you requested "
            "33192 tokens (25000 in the messages, 8192 in the completion). Please reduce the length of the messages "
            "or completion.",
            True,
        ),
        (  # a small history over by a little: half of it makes the request fit
            "openai API error: 400 This model's maximum context length is 32768 tokens. However, you requested "
            "33000 tokens (1000 in the messages, 32000 in the completion). Please reduce the length of the messages "
            "or completion.",
            True,
        ),
        (  # no history can make room for a completion as big as the window: cutting cannot help
            "openai API error: 400 This model's maximum context length is 32768 tokens. However, you requested "
            "41768 tokens (1000 in the messages, 40768 in the completion). Please reduce the length of the messages "
            "or completion.",
            False,
        ),
        (  # the completion is exactly the window: not even an empty history fits
            "openai API error: 400 This model's maximum context length is 32768 tokens. However, you requested "
            "33768 tokens (1000 in the messages, 32768 in the completion). Please reduce the length of the messages "
            "or completion.",
            False,
        ),
        (  # Claude: a 175K history and a 32K max_tokens on a 200K window, the history fits on its own
            "Anthropic API error: 400 input length and `max_tokens` exceed context limit: 175000 + 32000 > 200000, "
            "decrease input length or `max_tokens` and try again",
            True,
        ),
        (  # max_tokens just under the window: a small enough history still fits
            "Anthropic API error: 400 input length and `max_tokens` exceed context limit: 3000 + 199000 > 200000, "
            "decrease input length or `max_tokens` and try again",
            True,
        ),
        (  # max_tokens as big as the window: not even an empty history fits
            "Anthropic API error: 400 input length and `max_tokens` exceed context limit: 3000 + 200000 > 200000, "
            "decrease input length or `max_tokens` and try again",
            False,
        ),
        (  # max_tokens bigger than the window
            "Anthropic API error: 400 input length and `max_tokens` exceed context limit: 3000 + 250000 > 200000, "
            "decrease input length or `max_tokens` and try again",
            False,
        ),
        ("Anthropic API error: 400 messages.1: `tool_use` ids were found without `tool_result` blocks", False),
        ("Anthropic API error: 429 rate limited", False),
        ("openai API error: 400 tool name is too long", False),
        ("Anthropic connection error: timed out", False),
    ],
)
def test_which_errors_say_the_history_is_too_long(message, expected):
    from pincer.core.agent import _is_context_length_error

    assert _is_context_length_error(LLMError(message)) is expected


_PAIR_ERROR = (
    "openai API error: 400 An assistant message with 'tool_calls' must be followed by tool messages "
    "responding to each 'tool_call_id'. The following tool_call_ids did not have response messages: late"
)
_DONE = LLMResponse(content="ok", model="m", input_tokens=10, output_tokens=5, stop_reason="end_turn")


def _orphaned_call() -> LLMMessage:
    return LLMMessage(
        role=MessageRole.ASSISTANT, content="", tool_calls=[ToolCall(id="late", name="greet", arguments={})]
    )


def _script(
    mock_llm, errors: list[str], *, orphan_on: frozenset[int] = frozenset(), session: Session | None = None
) -> list[int]:
    """`mock_llm` rejects its first requests with `errors` in turn, then answers. The requests numbered `orphan_on`
    (0-based) find an orphaned tool call in the history, as when a concurrent turn wrote one between the
    proactive repair and the call: it is written to `session`, which a cut request is not built from. Returns the
    history sizes of the requests, as they were sent."""
    sizes: list[int] = []

    async def complete(*, messages: list[LLMMessage], **_kwargs: object) -> LLMResponse:
        number = len(sizes)
        sizes.append(len(messages))
        if number in orphan_on:
            (session.messages if session is not None else messages).append(_orphaned_call())
        if number < len(errors):
            raise LLMError(errors[number])
        return _DONE

    async def stream_turn(**kwargs):
        response = await complete(**kwargs)
        yield StreamTurnEvent(text=response.content)
        yield StreamTurnEvent(response=response)

    mock_llm.complete.side_effect = complete
    mock_llm.stream_turn = stream_turn
    return sizes


async def _turn(agent: Agent, *, streaming: bool) -> str:
    if not streaming:
        return (await agent.handle_message("user1", "test", "hello", memory=False)).text
    chunks = [c async for c in agent.handle_message_stream("user1", "test", "hello", memory=False)]
    return "".join(c.content for c in chunks if c.type == StreamEventType.DONE)


@pytest.mark.parametrize("streaming", [False, True], ids=["handle_message", "handle_message_stream"])
async def test_a_pairing_error_after_two_cuts_is_repaired_and_does_not_clear_the_session(
    settings, session_manager, cost_tracker, registry, mock_llm, streaming
):
    """The cuts and the pair repair have their own retries: two cuts leave the repair its whole allowance."""
    session = await _long_session(session_manager)
    sizes = _script(mock_llm, [_TOO_LONG, _TOO_LONG, _PAIR_ERROR], orphan_on=frozenset({2}), session=session)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, registry)

    reply = await _turn(agent, streaming=streaming)

    assert reply == "ok"
    assert len(sizes) == 4  # two cuts, one repair, then it goes through
    assert len(session.messages) > 8  # a quarter of the history and the turn, not a clear (SYSTEM only)
    assert "late" not in [tc.id for m in session.messages for tc in m.tool_calls]


@pytest.mark.parametrize("streaming", [False, True], ids=["handle_message", "handle_message_stream"])
async def test_two_repairs_leave_all_four_cuts(settings, session_manager, cost_tracker, registry, mock_llm, streaming):
    session = await _long_session(session_manager, pairs=100)
    sizes = _script(
        mock_llm, [_PAIR_ERROR, _PAIR_ERROR] + [_TOO_LONG] * 4, orphan_on=frozenset({0, 1}), session=session
    )
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, registry)

    reply = await _turn(agent, streaming=streaming)

    assert reply == "ok"
    assert len(sizes) == 7  # two repairs, four cuts, then it goes through


async def test_the_voice_path_counts_cuts_and_repairs_apart_too(
    settings, session_manager, cost_tracker, registry, mock_llm
):
    """A voice utterance retries through the same `_recover_from_rejected_history`, with the counts of its own turn."""
    from pincer.core.agent import _PairRecovery, _Recovery

    session = await _long_session(session_manager)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, registry)
    recovery = _Recovery()

    for _ in range(2):
        assert await agent._recover_from_rejected_history(session, LLMError(_TOO_LONG), recovery) is _PairRecovery.RETRY
    session.messages.append(_orphaned_call())
    assert await agent._recover_from_rejected_history(session, LLMError(_PAIR_ERROR), recovery) is _PairRecovery.RETRY

    assert (recovery.shrinks, recovery.repairs) == (2, 1)
    assert len(session.messages) > 8  # not cleared


_COMPLETION_BIGGER_THAN_THE_WINDOW = (
    "openai API error: 400 This model's maximum context length is 32768 tokens. However, you requested 41768 tokens "
    "(1000 in the messages, 40768 in the completion). Please reduce the length of the messages or completion."
)
_MESSAGES_FIT_BUT_NOT_WITH_THE_COMPLETION = (
    "openai API error: 400 This model's maximum context length is 32768 tokens. However, you requested 33192 tokens "
    "(25000 in the messages, 8192 in the completion). Please reduce the length of the messages or completion."
)


async def test_a_completion_budget_no_history_can_make_room_for_cuts_nothing_and_saves_nothing(
    settings, session_manager, cost_tracker, registry, mock_llm
):
    session = await _long_session(session_manager)
    before = list(session.messages)
    sizes = _script(mock_llm, [_COMPLETION_BIGGER_THAN_THE_WINDOW] * 5)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, registry)

    with pytest.raises(LLMError, match="in the completion"):
        await agent.handle_message("user1", "test", "hello", memory=False)

    assert sizes == [len(before) + 1]  # one request: halving could not have helped
    assert session.messages[: len(before)] == before
    row = await session_manager._store.load("test:user1", user_id="user1", channel="test", legacy_fallback=False)
    assert row is not None
    assert len(json.loads(row["messages"])) == len(before) + 1


_CLAUDE_HISTORY_PLUS_MAX_TOKENS = (
    "input length and `max_tokens` exceed context limit: 175000 + 32000 > 200000, "
    "decrease input length or `max_tokens` and try again"
)


@pytest.mark.parametrize("streaming", [False, True], ids=["handle_message", "handle_message_stream"])
async def test_claudes_input_plus_max_tokens_error_cuts_the_history_and_goes_through(
    settings, session_manager, cost_tracker, registry, streaming
):
    """A 175K history fits a 200K window alone, but not beside a 32K `max_tokens`: Claude says so in its own
    words, and halving the history is what makes the request fit."""
    session = await _long_session(session_manager)
    before = len(session.messages)
    async with ScriptedAnthropic(settings, [Reject(400, _CLAUDE_HISTORY_PLUS_MAX_TOKENS), _OK]) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry)
        reply = await _turn(agent, streaming=streaming)

    assert reply == "Done."
    assert len(llm.requests) == 2
    assert _sent(llm.requests[1]) < _sent(llm.requests[0]) * 0.6
    assert len(session.messages) < before * 0.6 + 2


async def test_claudes_max_tokens_as_big_as_the_window_cuts_nothing_and_saves_nothing(
    settings, session_manager, cost_tracker, registry, mock_llm
):
    session = await _long_session(session_manager)
    before = list(session.messages)
    no_room = (
        "Anthropic API error: 400 input length and `max_tokens` exceed context limit: 3000 + 200000 > 200000, "
        "decrease input length or `max_tokens` and try again"
    )
    sizes = _script(mock_llm, [no_room] * 5)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, registry)

    with pytest.raises(LLMError, match="max_tokens"):
        await agent.handle_message("user1", "test", "hello", memory=False)

    assert sizes == [len(before) + 1]  # one request: halving could not have helped
    assert session.messages[: len(before)] == before


@pytest.mark.parametrize("streaming", [False, True], ids=["handle_message", "handle_message_stream"])
async def test_a_cut_that_let_a_request_through_stays_when_a_later_request_of_the_turn_fails(
    settings, session_manager, cost_tracker, registry, mock_llm, tool_registry, streaming
):
    """The history put back on a give-up is the one before the cuts, but only while no request has gone
    through: after one did, a later error (a tool round's) must not undo cuts the turn already built on."""
    session = await _long_session(session_manager)
    before = len(session.messages)
    call = ToolCall(id="t1", name="greet", arguments={"name": "Ann"})
    requests = 0

    async def complete(**_kwargs: object) -> LLMResponse:
        nonlocal requests
        requests += 1
        if requests == 1:
            raise LLMError(_TOO_LONG)
        if requests == 2:
            return LLMResponse(
                content="", model="m", input_tokens=1, output_tokens=1, stop_reason="tool_use", tool_calls=[call]
            )
        raise LLMError("openai API error: 500 server error")

    async def stream_turn(**kwargs):
        response = await complete(**kwargs)
        yield StreamTurnEvent(response=response)

    mock_llm.complete.side_effect = complete
    mock_llm.stream_turn = stream_turn
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    with pytest.raises(LLMError, match="server error"):
        await _turn(agent, streaming=streaming)

    assert requests == 3
    assert len(session.messages) < before * 0.6  # the cut stayed
    assert [m.role for m in session.messages[-3:]] == [MessageRole.USER, MessageRole.ASSISTANT, MessageRole.TOOL_RESULT]
    row = await session_manager._store.load("test:user1", user_id="user1", channel="test", legacy_fallback=False)
    assert row is not None
    assert len(json.loads(row["messages"])) == len(session.messages)


async def test_a_turn_that_gives_up_on_the_cuts_for_another_error_puts_the_history_back(
    settings, session_manager, cost_tracker, registry, mock_llm
):
    """The retry after a cut fails with something else (a rate limit): the cut was never shown to work."""
    session = await _long_session(session_manager)
    before = list(session.messages)
    _script(mock_llm, [_TOO_LONG, "openai API error: 429 rate limited"])
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, registry)

    with pytest.raises(LLMError, match="rate limited"):
        await agent.handle_message("user1", "test", "hello", memory=False)

    assert session.messages[: len(before)] == before


async def test_a_voice_cut_that_let_a_request_through_stays_when_a_later_request_fails(
    settings, session_manager, cost_tracker, tool_registry
):
    from anthropic_sse import tool_use

    await session_manager.onboarding.complete("user1", "voice", now=1.0)
    session = await _long_session(session_manager, key="voice:CA_long")
    before = len(session.messages)
    script = [Reject(400, _TOO_LONG), ([tool_use("t1", "greet", {"name": "Ann"})], "tool_use"), Reject(400, "boom")]
    async with ScriptedAnthropic(settings, script) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, tool_registry)
        with pytest.raises(LLMError):
            _ = [c async for c in agent.stream_voice_turn("user1", "voice", "hello", session_key="voice:CA_long")]

    assert len(llm.requests) == 3
    assert len(session.messages) < before * 0.6  # the cut stayed
    assert session.messages[-1].role is MessageRole.TOOL_RESULT


async def test_messages_that_fit_on_their_own_but_not_with_the_completion_are_cut_and_go_through(
    settings, session_manager, cost_tracker, registry, mock_llm
):
    """The usual first failure of a growing history: 25,000 tokens in a 32K window with 8,192 reserved for the
    reply. Every later turn adds a little, so a turn that raised here would raise on every later one."""
    session = await _long_session(session_manager)
    before = len(session.messages)
    sizes = _script(mock_llm, [_MESSAGES_FIT_BUT_NOT_WITH_THE_COMPLETION])
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, registry)

    reply = await agent.handle_message("user1", "test", "hello", memory=False)

    assert reply.text == "ok"
    assert len(sizes) == 2
    assert sizes[1] < sizes[0] * 0.6
    assert len(session.messages) < before * 0.6


# ── a cut is applied to the session only once a request goes through (PR #379 review, round 3) ──


async def _stored_messages(sessions: SessionManager, key: str = "test:user1", channel: str = "test") -> int:
    row = await sessions._store.load(key, user_id="user1", channel=channel, legacy_fallback=False)
    assert row is not None
    return len(json.loads(row["messages"]))


async def test_a_cut_is_not_saved_by_the_pair_repair_or_by_a_turn_that_ends_in_an_exception(
    settings, session_manager, cost_tracker, registry, mock_llm
):
    """The pair repair at the top of every attempt saves the session; it must save the history, not the cut."""
    session = await _long_session(session_manager, photo=True)
    before = list(session.messages)
    requests = 0

    async def complete(*, messages: list[LLMMessage], **_kwargs: object) -> LLMResponse:
        nonlocal requests
        requests += 1
        if requests == 1:
            messages.append(_orphaned_call())  # the repair has something to save at the next attempt
            raise LLMError(_TOO_LONG)
        raise RuntimeError("boom")

    mock_llm.complete.side_effect = complete
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, registry)

    with pytest.raises(RuntimeError, match="boom"):
        await agent.handle_message("user1", "test", "hello", memory=False)

    assert requests == 2
    assert session.messages[: len(before)] == before
    assert await _stored_messages(session_manager) == len(before) + 1  # the question; the orphan was repaired away
    assert await _stored_photos(session_manager, "test:user1") == 1  # the photo of the turn the cut would drop


async def test_a_turn_that_runs_out_of_rounds_after_cuts_saves_its_reply_on_the_whole_history(
    settings, session_manager, cost_tracker, registry, mock_llm
):
    settings.max_tool_iterations = 2
    session = await _long_session(session_manager, photo=True)
    before = list(session.messages)
    _script(mock_llm, [_TOO_LONG, _TOO_LONG])
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, registry)

    await agent.handle_message("user1", "test", "hello", memory=False)  # ends with its "no answer" reply

    assert session.messages[: len(before)] == before
    assert await _stored_photos(session_manager, "test:user1") == 1


@pytest.mark.parametrize("path", ["stream", "voice"])
async def test_a_stream_that_ends_without_a_final_response_after_a_cut_leaves_the_history_whole(
    settings, session_manager, cost_tracker, registry, mock_llm, path
):
    key = "voice:CA_long" if path == "voice" else None
    if path == "voice":
        await session_manager.onboarding.complete("user1", "voice", now=1.0)
    session = await _long_session(session_manager, key=key)
    before = list(session.messages)
    requests = 0

    async def stream_turn(**_kwargs: object):
        nonlocal requests
        requests += 1
        if requests == 1:
            raise LLMError(_TOO_LONG)
        return
        yield  # pragma: no cover - makes this an async generator that ends at once

    mock_llm.stream_turn = stream_turn
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, registry)

    with pytest.raises(LLMError, match="without a final response"):
        if path == "stream":
            _ = [c async for c in agent.handle_message_stream("user1", "test", "hello", memory=False)]
        else:
            _ = [c async for c in agent.stream_voice_turn("user1", "voice", "hello", session_key=key)]

    assert requests == 2
    assert session.messages[: len(before)] == before


async def test_a_turn_that_cuts_a_joined_history_keeps_the_summaries_of_both_conversations(
    settings, session_manager, cost_tracker, registry, mock_llm
):
    """Two histories joined by an identity rename: [summary, first turns, summary, second turns]."""
    session = await session_manager.get_or_create("user1", "test")
    first = LLMMessage(role=MessageRole.SYSTEM, content="[Previous conversation summary]\nFIRST")
    second = LLMMessage(role=MessageRole.SYSTEM, content="[Previous conversation summary]\nSECOND")
    turns = []
    for label in ("one", "two"):
        for n in range(10):
            turns.append(LLMMessage(role=MessageRole.USER, content=f"{label} question {n} " + "q" * 380))
            turns.append(LLMMessage(role=MessageRole.ASSISTANT, content=f"{label} answer {n} " + "a" * 380))
    await session_manager.add_messages(session, [first, *turns[:20], second, *turns[20:]])
    _script(mock_llm, [_TOO_LONG])
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, registry)

    await agent.handle_message("user1", "test", "hello", memory=False)

    assert session.messages[0] is first
    assert session.messages[1] is second
    assert session.messages[2].role is MessageRole.USER
    assert session.messages[2].content.startswith("two question ")  # the cut went past the second summary
    assert len(session.messages) < 30


# ── "half" means half of what goes on the wire: stored output of another wire is not sent (round 3) ──


async def _session_after_a_model_switch(sessions: SessionManager, wire: str, raw: list[dict[str, Any]]) -> Session:
    """A summary kept from another provider (its stored output is big, and sent to no one else), then 20 pairs."""
    session = await sessions.get_or_create("user1", "test")
    summary = LLMMessage(role=MessageRole.SYSTEM, content="[Previous conversation summary]\nSUM", raw=raw, wire=wire)
    await sessions.add_message(session, summary)
    for pair in range(_PAIRS):
        await sessions.add_message(session, LLMMessage(role=MessageRole.USER, content=f"question {pair} " + "q" * 380))
        await sessions.add_message(
            session, LLMMessage(role=MessageRole.ASSISTANT, content=f"answer {pair} " + "a" * 380)
        )
    return session


_BIG_WINDOW = [{"type": "compaction", "id": "cmp_1", "encrypted_content": "E" * 400_000}]
_BIG_BLOCK = [{"type": "compaction", "content": "S" * 400_000, "signature": "SIG"}]


def _about_half_went(session: Session, before: int) -> None:
    kept = len(session.messages) - 2  # without the new question and the reply
    assert before * 0.4 < kept < before * 0.6, f"{kept} of {before} kept"


@pytest.mark.parametrize("streaming", [False, True], ids=["handle_message", "handle_message_stream"])
async def test_the_halving_on_an_anthropic_endpoint_ignores_the_window_of_another_provider(
    settings, session_manager, cost_tracker, registry, streaming
):
    session = await _session_after_a_model_switch(session_manager, "openai:gpt-6", _BIG_WINDOW)
    before = len(session.messages)
    async with ScriptedAnthropic(settings, [Reject(400, _TOO_LONG), _OK]) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry)
        reply = await _turn(agent, streaming=streaming)

    assert reply == "Done."
    assert len(llm.requests) == 2
    _about_half_went(session, before)  # the 400,000 characters of the window were never going out


@pytest.mark.parametrize("path", ["complete", "stream responses", "stream chat"])
async def test_the_halving_on_an_openai_endpoint_ignores_the_block_of_another_provider(
    settings, session_manager, cost_tracker, registry, path
):
    from openai_wire import ScriptedOpenAI, Status, message_item
    from pydantic import SecretStr

    openai_settings = settings.model_copy(
        update={
            "default_provider": "openai",
            "openai_model": "gpt-5.2",
            "openai_api_key": SecretStr("sk-test"),
            "reasoning_effort": "",
            "thinking": "",
            **({"responses_api_providers": []} if path == "stream chat" else {}),
        }
    )
    session = await _session_after_a_model_switch(session_manager, "anthropic:claude-haiku-5-5", _BIG_BLOCK)
    before = len(session.messages)
    too_long = Status(400, {"error": {"message": "context_length_exceeded", "type": "invalid_request_error"}})
    llm = ScriptedOpenAI(openai_settings, [too_long, "Done." if path == "stream chat" else [message_item("Done.")]])
    agent = Agent(openai_settings, llm.provider, session_manager, cost_tracker, registry)

    reply = await _turn(agent, streaming=path != "complete")

    assert reply == "Done."
    assert len(llm.requests) == 2
    _about_half_went(session, before)


def test_an_llm_error_says_which_wire_it_came_from_when_the_provider_knows():
    assert LLMError("x").wire == ""
    assert LLMError("x", wire="anthropic:claude-haiku-5-5").wire == "anthropic:claude-haiku-5-5"


# ── no cut when the cuts cannot take off what the endpoint says is over (PR #379 review, round 5) ──

_OVER_BY_2000_CLAUDE = (
    "Anthropic API error: 400 input length and `max_tokens` exceed context limit: 3000 + 199000 > 200000, "
    "decrease input length or `max_tokens` and try again"
)
_OVER_BY_2232_OPENAI = (
    "openai API error: 400 This model's maximum context length is 32768 tokens. However, you requested 35000 tokens "
    "(3000 in the messages, 32000 in the completion). Please reduce the length of the messages or completion."
)


@pytest.mark.parametrize("error", [_OVER_BY_2000_CLAUDE, _OVER_BY_2232_OPENAI], ids=["claude", "openai"])
@pytest.mark.parametrize("streaming", [False, True], ids=["handle_message", "handle_message_stream"])
async def test_a_history_that_is_nearly_all_current_turn_is_not_cut_for_a_request_over_by_more_than_the_rest(
    settings, session_manager, cost_tracker, registry, mock_llm, error, streaming
):
    """The request is over by 2,000+ tokens of a 3,000-token input, but 150K characters of that input are the user's
    message of this turn, which no cut reaches: the earlier turns are a few percent of the history, so halving
    them cannot help and every turn would burn four rejected requests."""
    session = await _long_session(session_manager)
    before = list(session.messages)
    sizes = _script(mock_llm, [error] * 5)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, registry)

    with pytest.raises(LLMError, match="exceed context limit|in the completion"):
        if streaming:
            _ = [c async for c in agent.handle_message_stream("user1", "test", "x" * 150_000, memory=False)]
        else:
            await agent.handle_message("user1", "test", "x" * 150_000, memory=False)

    assert sizes == [len(before) + 1]  # one request: nothing a cut could remove would have been enough
    assert session.messages[: len(before)] == before


async def test_a_request_over_by_less_than_the_earlier_turns_hold_is_still_cut(
    settings, session_manager, cost_tracker, registry, mock_llm
):
    session = await _long_session(session_manager)
    before = len(session.messages)
    sizes = _script(mock_llm, [_OVER_BY_2000_CLAUDE.replace("3000 + 199000", "175000 + 32000")])
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, registry)

    await agent.handle_message("user1", "test", "hello", memory=False)

    assert len(sizes) == 2 and sizes[1] < sizes[0] * 0.6  # the control: it halved and went through
    assert len(session.messages) < before
