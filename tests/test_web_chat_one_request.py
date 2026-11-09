"""Web chat sends one request per step (issue #357 slice 13).

Every step of a streamed turn is one `stream_turn` request with the turn's tools and system
prompt: its text streams as it is generated, its tool calls run, and the step without tool
calls is the reply. Nothing is asked twice.

`test-fixture-model` costs $7 per MTok for every token type; a scripted Anthropic response
uses 10 input + 5 output tokens, so one request costs $0.000105.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from anthropic_sse import ScriptedAnthropic, text, thinking, tool_use

from pincer.core.agent import Agent
from pincer.db.engine import get_database_url
from pincer.db.session import session_scope
from pincer.exceptions import LLMRateLimitError
from pincer.llm.base import BaseLLMProvider, LLMResponse, MessageRole, StreamTurnEvent, ToolCall
from pincer.repositories.costs import CostLogRepository
from pincer.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

_MODEL = "test-fixture-model"
_REQUEST_COST = 15 * 7.0 / 1e6


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    async def lookup(query: str = "") -> str:
        return f"found {query}"

    registry.register(name="lookup", description="Look something up", handler=lookup)
    return registry


@pytest.fixture
def fixture_settings(settings):  # type: ignore[no-untyped-def]
    settings.default_model = _MODEL
    return settings


async def _web_chat(agent: Agent, words: str = "hi") -> list[tuple[str, dict[str, Any]]]:
    """One turn through the real `/api/chat/stream` endpoint: its SSE events, in order."""
    from pincer.api.chat import chat_user
    from pincer.api.server import create_app

    app = create_app()
    app.state.agent = agent
    app.dependency_overrides[chat_user] = lambda: "user"
    body = {"text": words, "conversation_id": str(uuid.uuid4()), "memory": False}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/chat/stream", json=body)
    assert response.status_code == 200
    events = []
    for frame in response.text.split("\n\n"):
        if frame.strip():
            event, data = frame.split("\n", 1)
            events.append((event.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return events


async def _stored_costs(tmp_path: Path) -> list[float]:
    async with session_scope(get_database_url(tmp_path / "test.db")) as session:
        rows = await CostLogRepository(session).list()
    return [r.cost_usd for r in sorted(rows, key=lambda r: r.timestamp)]


async def test_a_plain_web_chat_turn_sends_one_request_with_the_tools(  # type: ignore[no-untyped-def]
    fixture_settings, session_manager, cost_tracker, tmp_path
) -> None:
    async with ScriptedAnthropic(fixture_settings, [([text("Hello there")], "end_turn")]) as llm:
        agent = Agent(fixture_settings, llm.provider, session_manager, cost_tracker, _registry())
        events = await _web_chat(agent)

    assert len(llm.requests) == 1
    assert [t["name"] for t in llm.requests[0]["tools"]] == ["lookup"]
    assert events == [
        ("chunk", {"delta": "Hello there"}),
        ("done", {"text": "Hello there", "cost_usd": pytest.approx(_REQUEST_COST)}),
    ]
    assert await _stored_costs(tmp_path) == [pytest.approx(_REQUEST_COST)]


def _raw(raw_request: str, key: str) -> str:
    """One top-level field exactly as it went over the wire."""
    return json.dumps(json.loads(raw_request)[key], ensure_ascii=False, separators=(",", ":"))


def _raw_messages(raw_request: str) -> list[str]:
    return [json.dumps(m, ensure_ascii=False, separators=(",", ":")) for m in json.loads(raw_request)["messages"]]


_TOOL_ROUND = [
    ([tool_use("toolu_1", "lookup", {"query": "tea"})], "tool_use"),
    ([text("Green tea it is.")], "end_turn"),
]


async def test_one_tool_round_sends_two_requests_with_one_prefix(  # type: ignore[no-untyped-def]
    fixture_settings, session_manager, cost_tracker, tmp_path
) -> None:
    async with ScriptedAnthropic(fixture_settings, list(_TOOL_ROUND)) as llm:
        agent = Agent(fixture_settings, llm.provider, session_manager, cost_tracker, _registry())
        events = await _web_chat(agent, "which tea?")

    first, second = llm.raw_requests
    for key in ("tools", "system", "cache_control"):
        assert _raw(second, key) == _raw(first, key)
    assert _raw_messages(second)[: len(_raw_messages(first))] == _raw_messages(first)
    assert len(_raw_messages(second)) == len(_raw_messages(first)) + 2  # the tool call and its result
    assert events == [
        ("tool", {"phase": "start", "name": "Using lookup..."}),
        ("tool", {"phase": "done", "name": "lookup"}),
        ("chunk", {"delta": "Green tea it is."}),
        ("done", {"text": "Green tea it is.", "cost_usd": pytest.approx(2 * _REQUEST_COST)}),
    ]
    assert await _stored_costs(tmp_path) == [pytest.approx(_REQUEST_COST)] * 2


async def test_text_before_a_tool_call_streams_and_the_reply_follows_after_a_blank_line(  # type: ignore[no-untyped-def]
    fixture_settings, session_manager, cost_tracker
) -> None:
    replies = [
        ([text("Let me look."), tool_use("toolu_1", "lookup", {"query": "tea"})], "tool_use"),
        ([text("Green tea it is.")], "end_turn"),
    ]
    async with ScriptedAnthropic(fixture_settings, replies) as llm:
        agent = Agent(fixture_settings, llm.provider, session_manager, cost_tracker, _registry())
        chunks = [c async for c in agent.handle_message_stream("user1", "web", "which tea?", memory=False)]

    assert [(c.type.value, c.content) for c in chunks] == [
        ("text", "Let me look."),
        ("tool_start", "Using lookup..."),
        ("tool_done", "lookup"),
        ("text", "\n\n"),
        ("text", "Green tea it is."),
        ("done", "Let me look.\n\nGreen tea it is."),
    ]
    session = await session_manager.get_or_create("user1", "web")
    tool_call, _result, final = session.messages[-3:]
    assert (tool_call.content, [tc.name for tc in tool_call.tool_calls]) == ("Let me look.", ["lookup"])
    assert (final.role.value, final.content) == ("assistant", "Green tea it is.")  # said once in history


async def test_the_next_step_of_a_turn_sends_the_turns_thinking_back(  # type: ignore[no-untyped-def]
    fixture_settings, session_manager, cost_tracker
) -> None:
    """No tool-less request, so no history edit: preserved thinking reaches every later step (#345)."""
    replies = [
        ([thinking("Tea first.", "SIG-1"), tool_use("toolu_1", "lookup", {"query": "tea"})], "tool_use"),
        ([text("Green tea it is.")], "end_turn"),
    ]
    async with ScriptedAnthropic(fixture_settings, replies) as llm:
        agent = Agent(fixture_settings, llm.provider, session_manager, cost_tracker, _registry())
        chunks = [c async for c in agent.handle_message_stream("user1", "web", "which tea?", memory=False)]

    assert chunks[-1].content == "Green tea it is."
    tool_call_message = llm.requests[1]["messages"][-2]
    assert tool_call_message["role"] == "assistant"
    assert tool_call_message["content"][0] == {"type": "thinking", "thinking": "Tea first.", "signature": "SIG-1"}
    session = await session_manager.get_or_create("user1", "web")
    assert all(not m.thinking_blocks for m in session.messages)  # the turn's end drops it


class _RateLimitedOnce(BaseLLMProvider):
    """The first request is refused with a 429 before any event; the next one answers."""

    def __init__(self) -> None:
        self.requests = 0

    async def complete(self, *args: Any, **kwargs: Any) -> LLMResponse:
        raise AssertionError("web chat must not use complete()")

    async def stream_turn(self, *args: Any, **kwargs: Any) -> AsyncIterator[StreamTurnEvent]:
        self.requests += 1
        if self.requests == 1:
            raise LLMRateLimitError(retry_after=0.0)
        yield StreamTurnEvent(text="Hello there")
        yield StreamTurnEvent(
            response=LLMResponse(
                content="Hello there", request_model=_MODEL, input_tokens=10, output_tokens=5, stop_reason="end_turn"
            )
        )

    async def close(self) -> None:
        pass


async def test_a_step_rate_limited_before_its_first_event_is_retried(  # type: ignore[no-untyped-def]
    fixture_settings, session_manager, cost_tracker, tmp_path, caplog
) -> None:
    """Nothing has streamed yet, so asking again is safe (as `complete` retries a 429)."""
    llm = _RateLimitedOnce()
    agent = Agent(fixture_settings, llm, session_manager, cost_tracker, _registry())

    with caplog.at_level("WARNING", logger="pincer.core.agent"):
        chunks = [c async for c in agent.handle_message_stream("user1", "web", "hi", memory=False)]

    assert llm.requests == 2
    assert chunks[-1].content == "Hello there"
    assert await _stored_costs(tmp_path) == [pytest.approx(_REQUEST_COST)]
    assert "Rate limited before the first event, retrying in 0.0s (attempt 1)" in caplog.text


async def test_a_budget_stop_after_streamed_text_follows_it_after_a_blank_line(  # type: ignore[no-untyped-def]
    fixture_settings, session_manager, tmp_path
) -> None:
    """The tool step crosses the budget (one request = the whole budget): the next step is refused."""
    from pincer.config.pricing import load_price_table
    from pincer.llm.cost_tracker import CostTracker

    tracker = CostTracker(tmp_path / "test.db", daily_budget=_REQUEST_COST, prices=load_price_table())
    await tracker.initialize()
    settings = fixture_settings.model_copy(update={"daily_budget_usd": _REQUEST_COST})
    replies = [([text("Let me look."), tool_use("toolu_1", "lookup", {"query": "tea"})], "tool_use")]
    try:
        async with ScriptedAnthropic(settings, replies) as llm:
            agent = Agent(settings, llm.provider, session_manager, tracker, _registry())
            chunks = [c async for c in agent.handle_message_stream("user1", "web", "which tea?", memory=False)]
    finally:
        await tracker.close()

    budget_text = chunks[-1].content.removeprefix("Let me look.\n\n")
    assert budget_text.startswith("Warning: Daily budget limit reached.")
    assert [c.content for c in chunks if c.type.value == "text"] == ["Let me look.", "\n\n", budget_text]
    session = await session_manager.get_or_create("user1", "web")
    assert session.messages[-1].content == budget_text


class _Steps(BaseLLMProvider):
    """Each request streams the next scripted response: its text as one event, then the response."""

    def __init__(self, *responses: LLMResponse) -> None:
        self.responses = list(responses)
        self.requests = 0

    async def complete(self, *args: Any, **kwargs: Any) -> LLMResponse:
        raise AssertionError("web chat must not use complete()")

    async def stream_turn(self, *args: Any, **kwargs: Any) -> AsyncIterator[StreamTurnEvent]:
        self.requests += 1
        response = self.responses.pop(0)
        if response.content:
            yield StreamTurnEvent(text=response.content)
        yield StreamTurnEvent(response=response)

    async def close(self) -> None:
        pass


def _calls(tool: str, content: str = "", n: int = 1) -> LLMResponse:
    return LLMResponse(
        content=content,
        tool_calls=[ToolCall(id=f"tc{n}", name=tool, arguments={"query": str(n)})],
        request_model=_MODEL,
        input_tokens=10,
        output_tokens=5,
        stop_reason="tool_use",
    )


async def _turn(agent: Agent) -> tuple[list[str], str]:
    """(the TEXT chunks, the DONE text) of one streamed turn; DONE always equals the chunks joined."""
    chunks = [c async for c in agent.handle_message_stream("user1", "web", "which tea?", memory=False)]
    texts = [c.content for c in chunks if c.type.value == "text"]
    assert chunks[-1].type.value == "done"
    assert chunks[-1].content == "".join(texts)
    return texts, chunks[-1].content


@pytest.mark.parametrize(
    ("last_text", "expected"),
    [("", ["Max iterations reached."]), ("Still looking.", ["Still looking."])],
    ids=["canned", "last-step-text"],
)
async def test_the_iteration_limit_ends_the_turn_without_another_request(  # type: ignore[no-untyped-def]
    fixture_settings, session_manager, cost_tracker, last_text, expected
) -> None:
    settings = fixture_settings.model_copy(update={"max_tool_iterations": 2})
    llm = _Steps(_calls("lookup", n=1), _calls("lookup", last_text, n=2))
    agent = Agent(settings, llm, session_manager, cost_tracker, _registry())

    texts, _done = await _turn(agent)

    assert llm.requests == 2
    assert texts == expected
    session = await session_manager.get_or_create("user1", "web")
    assert session.messages[-1].role is MessageRole.TOOL_RESULT  # nothing saved after the tools, as before


@pytest.mark.parametrize(
    ("last_text", "expected"),
    [
        ("", ["I'm having repeated tool failures. Let me respond with what I have."]),
        ("Trying again.", ["Trying again."]),
    ],
    ids=["canned", "last-step-text"],
)
async def test_the_circuit_breaker_ends_the_turn_without_another_request(  # type: ignore[no-untyped-def]
    fixture_settings, session_manager, cost_tracker, last_text, expected
) -> None:
    """Three steps in a row whose tools fail. The reply is saved after the tool results, as before."""
    llm = _Steps(_calls("nope", n=1), _calls("nope", n=2), _calls("nope", last_text, n=3))
    agent = Agent(fixture_settings, llm, session_manager, cost_tracker, _registry())

    texts, _done = await _turn(agent)

    assert llm.requests == 3
    assert texts == expected
    session = await session_manager.get_or_create("user1", "web")
    assert [m.role for m in session.messages[-2:]] == [MessageRole.TOOL_RESULT, MessageRole.ASSISTANT]
    assert session.messages[-1].content == expected[0]


class _RateLimitedMidStream(_RateLimitedOnce):
    """A 429 after the first text: asking again would stream that text twice."""

    async def stream_turn(self, *args: Any, **kwargs: Any) -> AsyncIterator[StreamTurnEvent]:
        self.requests += 1
        yield StreamTurnEvent(text="Hel")
        raise LLMRateLimitError(retry_after=0.0)


async def test_a_step_rate_limited_after_its_first_event_is_not_asked_again(  # type: ignore[no-untyped-def]
    fixture_settings, session_manager, cost_tracker
) -> None:
    llm = _RateLimitedMidStream()
    agent = Agent(fixture_settings, llm, session_manager, cost_tracker, _registry())

    texts, done = await _turn(agent)

    assert llm.requests == 1
    assert (texts, done) == (["Hel"], "Hel")  # the text the user saw is kept as the reply
