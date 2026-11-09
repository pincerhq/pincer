"""Anthropic prompt caching: tools and system for 1 hour, the conversation automatically for 5 minutes
(issue #357 slice 10, owner ruling Q17)."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

import pytest
from anthropic_sse import ScriptedAnthropic, text, tool_use

from pincer.config.pricing import load_price_table
from pincer.core.agent import Agent, StreamEventType
from pincer.db.engine import get_database_url
from pincer.db.session import session_scope
from pincer.llm.base import LLMMessage, MessageRole
from pincer.llm.cost_tracker import CostTracker
from pincer.repositories.costs import CostLogRepository
from pincer.tools.registry import ToolRegistry

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ONE_HOUR = {"type": "ephemeral", "ttl": "1h"}
_AUTOMATIC = {"type": "ephemeral"}


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    async def lookup(query: str = "") -> str:
        return f"found {query}"

    async def create_event(title: str = "") -> str:
        return "created"

    registry.register(name="lookup", description="Look something up", handler=lookup)
    registry.register(name="create_event", description="Create a calendar event", handler=create_event)
    return registry


def _markers(value: Any) -> int:
    """How many `cache_control` keys the JSON value holds, at any depth."""
    if isinstance(value, dict):
        return ("cache_control" in value) + sum(_markers(v) for v in value.values())
    if isinstance(value, list):
        return sum(_markers(v) for v in value)
    return 0


async def _onboarded(session_manager, user_id: str = "user1", channel: str = "test") -> None:
    await session_manager.onboarding.complete(user_id, channel, now=1.0)


@pytest.fixture
async def agent_llm(settings, session_manager, cost_tracker):
    """An agent with two tools whose Anthropic HTTP replies are scripted per test."""
    await _onboarded(session_manager)

    def build(replies: list[Any]) -> tuple[ScriptedAnthropic, Any]:
        llm = ScriptedAnthropic(settings, replies)
        return llm, lambda: Agent(settings, llm.provider, session_manager, cost_tracker, _registry())

    return build


async def test_a_chat_request_caches_tools_and_system_for_an_hour_and_the_conversation_automatically(agent_llm):
    llm, make_agent = agent_llm([([text("Hi")], "end_turn")])
    async with llm:
        await make_agent().handle_message("user1", "test", "hello", memory=False)

    [request] = llm.requests
    assert [t.get("cache_control") for t in request["tools"]] == [None, _ONE_HOUR]
    [system] = request["system"]
    assert system["type"] == "text"
    assert system["cache_control"] == _ONE_HOUR
    assert system["text"].strip()
    assert request["cache_control"] == _AUTOMATIC
    assert _markers(request["messages"]) == 0
    assert _markers(request) == 3


def _raw(raw_request: str, key: str) -> str:
    """One top-level field exactly as it went over the wire."""
    return json.dumps(json.loads(raw_request)[key], ensure_ascii=False, separators=(",", ":"))


def _raw_messages(raw_request: str) -> list[str]:
    return [json.dumps(m, ensure_ascii=False, separators=(",", ":")) for m in json.loads(raw_request)["messages"]]


async def test_each_request_of_a_three_turn_conversation_repeats_the_earlier_one_up_to_its_breakpoints(agent_llm):
    replies = [
        ([text("Reply 1")], "end_turn"),
        ([tool_use("toolu_1", "lookup", {"query": "tea"})], "tool_use"),
        ([text("Reply 2")], "end_turn"),
        ([text("Reply 3")], "end_turn"),
    ]
    llm, make_agent = agent_llm(replies)
    async with llm:
        agent = make_agent()
        for words in ("turn 1", "turn 2", "turn 3"):
            await agent.handle_message("user1", "test", words, memory=False)

    raw = llm.raw_requests
    assert len(raw) == 4
    assert [len(json.loads(r)["messages"]) for r in raw] == [1, 3, 5, 7]
    for earlier, later in zip(raw, raw[1:], strict=False):
        assert _raw(later, "tools") == _raw(raw[0], "tools")
        assert _raw(later, "system") == _raw(raw[0], "system")
        assert _raw(later, "cache_control") == _raw(raw[0], "cache_control")
        sent_before = _raw_messages(earlier)
        assert _raw_messages(later)[: len(sent_before)] == sent_before


_TOOLS = [
    {"name": "lookup", "description": "Look something up", "input_schema": {"type": "object"}},
    {"name": "create_event", "description": "Create an event", "input_schema": {"type": "object"}},
]


async def test_a_streamed_request_with_tools_carries_the_same_breakpoints(settings):
    async with ScriptedAnthropic(settings, [([text("Hi")], "end_turn")]) as llm:
        events = [
            e
            async for e in llm.provider.stream_turn(
                [LLMMessage(role=MessageRole.USER, content="hello")], tools=_TOOLS, system="You are Pincer."
            )
        ]

    assert events[-1].response is not None
    [request] = llm.requests
    assert [t.get("cache_control") for t in request["tools"]] == [None, _ONE_HOUR]
    assert request["system"] == [{"type": "text", "text": "You are Pincer.", "cache_control": _ONE_HOUR}]
    assert request["cache_control"] == _AUTOMATIC
    assert request["messages"] == [{"role": "user", "content": "hello"}]
    assert _TOOLS[-1].get("cache_control") is None  # the caller's schemas are never changed


async def test_a_one_shot_request_without_tools_is_not_cached(settings):
    """Summaries and extractions send no tools: a cache write there is never read back."""
    async with ScriptedAnthropic(settings, [([text("summary")], "end_turn")]) as llm:
        await llm.provider.complete(
            [LLMMessage(role=MessageRole.USER, content="Summarize this conversation")], system="You summarize."
        )

    [request] = llm.requests
    assert _markers(request) == 0
    assert request["system"] == "You summarize."
    assert "tools" not in request


async def test_prompt_cache_tools_off_sends_no_breakpoint(settings):
    settings.prompt_cache_tools = False
    async with ScriptedAnthropic(settings, [([text("Hi")], "end_turn")]) as llm:
        await llm.provider.complete(
            [LLMMessage(role=MessageRole.USER, content="hello")], tools=_TOOLS, system="You are Pincer."
        )

    [request] = llm.requests
    assert _markers(request) == 0
    assert request["system"] == "You are Pincer."
    assert [t["name"] for t in request["tools"]] == ["lookup", "create_event"]


# One tool round: the tool-call request writes the prefix, the answer reads it
_WRITE = {
    "input_tokens": 7,
    "cache_creation_input_tokens": 3000,
    "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 2900},
}
_READ = {
    "input_tokens": 3,
    "cache_read_input_tokens": 3100,
    "cache_creation_input_tokens": 40,
    "cache_creation": {"ephemeral_5m_input_tokens": 40, "ephemeral_1h_input_tokens": 0},
}
_TOOL_ROUND = [
    ([tool_use("toolu_1", "lookup", {"query": "tea"})], "tool_use", _WRITE),
    ([text("Green tea it is.")], "end_turn", _READ),
]


def _turn_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("Chat turn:")]


async def test_a_chat_turn_logs_its_cache_reads_and_writes(agent_llm, session_manager, caplog):
    llm, make_agent = agent_llm(_TOOL_ROUND)
    with caplog.at_level(logging.INFO, logger="pincer.core.agent"):
        async with llm:
            response = await make_agent().handle_message("user1", "test", "which tea?", memory=False)

    session = await session_manager.get_or_create("user1", "test")
    assert response.cost_usd > 0
    [line] = _turn_lines(caplog)
    assert line == (
        f"Chat turn: session={session.session_id} requests=2 input=10 cache_read=3100 "
        f"cache_write_5m=140 cache_write_1h=2900 output=10 cost=${response.cost_usd:.6f}"
    )


async def test_a_streamed_chat_turn_logs_every_request_including_the_final_stream(agent_llm, session_manager, caplog):
    final = ([text("Green tea it is.")], "end_turn", {"input_tokens": 3200})
    llm, make_agent = agent_llm([*_TOOL_ROUND, final])
    with caplog.at_level(logging.INFO, logger="pincer.core.agent"):
        async with llm:
            chunks = [c async for c in make_agent().handle_message_stream("user1", "test", "which tea?", memory=False)]

    session = await session_manager.get_or_create("user1", "test")
    done = chunks[-1]
    assert done.type is StreamEventType.DONE
    assert done.cost_usd > 0
    assert len(llm.requests) == 3
    [line] = _turn_lines(caplog)
    assert line == (
        f"Chat turn: session={session.session_id} requests=3 input=3210 cache_read=3100 "
        f"cache_write_5m=140 cache_write_1h=2900 output=15 cost=${done.cost_usd:.6f}"
    )


async def test_a_one_hour_write_is_stored_and_priced_at_the_one_hour_rate(settings, session_manager, tmp_path):
    """Shipped Haiku 5.5: 0.10 input / 0.20 1-hour write / 0.50 output per MTok (5-minute writes: 0.125)."""
    shipped = tmp_path / "shipped"
    shipped.mkdir()
    shutil.copy(_REPO_ROOT / "pincer.toml", shipped / "pincer.toml")
    costs = CostTracker(tmp_path / "priced.db", daily_budget=100.0, prices=load_price_table(shipped))
    await costs.initialize()
    settings.default_model = "claude-haiku-5-5"
    await _onboarded(session_manager)
    usage = {
        "input_tokens": 7,
        "cache_creation_input_tokens": 50_000,
        "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 50_000},
    }
    try:
        async with ScriptedAnthropic(settings, [([text("Hi")], "end_turn", usage)]) as llm:
            agent = Agent(settings, llm.provider, session_manager, costs, _registry())
            response = await agent.handle_message("user1", "test", "hello", memory=False)
    finally:
        await costs.close()

    async with session_scope(get_database_url(tmp_path / "priced.db")) as session:
        [row] = await CostLogRepository(session).list()
    assert (row.model, row.input_tokens, row.cache_write_5m_tokens, row.cache_write_1h_tokens) == (
        "claude-haiku-5-5",
        7,
        0,
        50_000,
    )
    assert row.cost_usd == pytest.approx((7 * 0.10 + 50_000 * 0.20 + 5 * 0.50) / 1e6)
    assert response.cost_usd == pytest.approx(0.0100032)
