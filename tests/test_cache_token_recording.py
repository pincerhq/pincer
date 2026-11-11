"""The agent records every token type of each call it makes (issue #357).

`test-fixture-model` costs $7 per MTok for every token type (tests/fixtures/pricing), so a
call's cost is 7 × all its tokens / 1M: a token type left out of what the agent passes to
the cost service changes the stored cost, not only the stored counts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

from pincer.config.pricing import TokenUsage
from pincer.db.engine import get_database_url
from pincer.db.session import session_scope
from pincer.llm.base import BaseLLMProvider, LLMResponse, StreamTurnEvent, ToolCall
from pincer.repositories.costs import CostLogRepository

_FIRST = TokenUsage(
    input_tokens=100, cache_write_5m_tokens=2_000, cache_write_1h_tokens=300, cache_read_tokens=40_000, output_tokens=50
)
_SECOND = TokenUsage(input_tokens=20, cache_read_tokens=42_000, output_tokens=30)
_FIRST_COST = 7 * 42_450 / 1_000_000  # 0.29715
_SECOND_COST = 7 * 42_050 / 1_000_000  # 0.29435


def _response(usage: TokenUsage, tool_calls: list[ToolCall] | None = None) -> LLMResponse:
    return LLMResponse(
        content="" if tool_calls else "Done.",
        tool_calls=tool_calls or [],
        request_model="test-fixture-model",
        stop_reason="tool_use" if tool_calls else "end_turn",
        input_tokens=usage.input_tokens,
        cache_write_5m_tokens=usage.cache_write_5m_tokens,
        cache_write_1h_tokens=usage.cache_write_1h_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        output_tokens=usage.output_tokens,
    )


def _scripted_llm(*responses: LLMResponse) -> AsyncMock:
    queue = list(responses)
    llm = AsyncMock(spec=BaseLLMProvider)
    llm.complete.side_effect = lambda *a, **k: queue.pop(0)

    async def stream_turn(*args: Any, **kwargs: Any) -> AsyncIterator[StreamTurnEvent]:
        response = queue.pop(0)
        if response.content:
            yield StreamTurnEvent(text=response.content)
        yield StreamTurnEvent(response=response)

    llm.stream_turn = stream_turn
    return llm


async def _stored(tmp_path) -> list[tuple[TokenUsage, float]]:  # type: ignore[no-untyped-def]
    async with session_scope(get_database_url(tmp_path / "test.db")) as session:
        rows = await CostLogRepository(session).list()
    return [
        (
            TokenUsage(
                input_tokens=r.input_tokens,
                cache_write_5m_tokens=r.cache_write_5m_tokens,
                cache_write_1h_tokens=r.cache_write_1h_tokens,
                cache_read_tokens=r.cache_read_tokens,
                output_tokens=r.output_tokens,
            ),
            r.cost_usd,
        )
        for r in sorted(rows, key=lambda r: r.timestamp)
    ]


async def test_a_chat_turn_records_each_call_s_tokens_and_reports_their_total(  # type: ignore[no-untyped-def]
    settings, session_manager, cost_tracker, tool_registry, tmp_path
) -> None:
    """The CLI footer prints `AgentResponse.cost_usd`: the sum of the turn's recorded costs."""
    from pincer.core.agent import Agent

    llm = _scripted_llm(
        _response(_FIRST, [ToolCall(id="tc1", name="greet", arguments={"name": "Ann"})]), _response(_SECOND)
    )
    agent = Agent(settings, llm, session_manager, cost_tracker, tool_registry)
    reply = await agent.handle_message("user", "chat", "hi", memory=False)

    stored = await _stored(tmp_path)
    assert [usage for usage, _ in stored] == [_FIRST, _SECOND]
    assert [cost for _, cost in stored] == [pytest.approx(_FIRST_COST), pytest.approx(_SECOND_COST)]
    assert reply.cost_usd == pytest.approx(sum(cost for _, cost in stored), abs=1e-12)
    assert reply.cost_usd == pytest.approx(0.5915, abs=1e-12)


@pytest.mark.parametrize("path", ["headless", "voice"])
async def test_headless_and_voice_calls_record_every_token_type(  # type: ignore[no-untyped-def]
    settings, session_manager, cost_tracker, tool_registry, tmp_path, path: str
) -> None:
    from pincer.core.agent import Agent

    agent = Agent(settings, _scripted_llm(_response(_FIRST)), session_manager, cost_tracker, tool_registry)
    if path == "headless":
        await agent.run_headless("hi", "user", "chat")
    else:
        [chunk async for chunk in agent.stream_voice_turn("user", "voice", "hi")]

    assert await _stored(tmp_path) == [(_FIRST, pytest.approx(_FIRST_COST))]


async def test_a_phone_call_is_billed_its_llm_cost_with_the_cache_tokens(cost_tracker) -> None:  # type: ignore[no-untyped-def]
    """Per-call attribution gets the recorded cost (all token types) and the uncached input."""
    from pincer.observability import call_costs

    call_costs.reset_for_tests()
    call_costs.begin_call("CA1")
    with call_costs.call_context("CA1"):
        await cost_tracker.record("anthropic", "test-fixture-model", _FIRST, session_id="s1")
    usage = call_costs.end_call("CA1")
    assert (usage.input_tokens, usage.output_tokens) == (100, 50)
    assert usage.cost_usd == pytest.approx(_FIRST_COST, abs=1e-12)


@pytest.mark.parametrize("path", ["chat", "stream", "headless", "voice"])
async def test_every_path_records_a_request_under_the_provider_that_served_it(  # type: ignore[no-untyped-def]
    settings, session_manager, cost_tracker, tool_registry, tmp_path, path: str
) -> None:
    """A failover answers from another provider than the configured default: the row names the one that served."""
    from dataclasses import replace

    from pincer.core.agent import Agent

    served = replace(_response(_FIRST), provider="served-by-failover")
    assert settings.default_provider != served.provider  # precondition: the two names differ
    agent = Agent(settings, _scripted_llm(served), session_manager, cost_tracker, tool_registry)
    if path == "chat":
        await agent.handle_message("user", "chat", "hi", memory=False)
    elif path == "stream":
        [chunk async for chunk in agent.handle_message_stream("user", "web", "hi", memory=False)]
    elif path == "headless":
        await agent.run_headless("hi", "user", "chat")
    else:
        [chunk async for chunk in agent.stream_voice_turn("user", "voice", "hi")]

    async with session_scope(get_database_url(tmp_path / "test.db")) as session:
        rows = await CostLogRepository(session).list()
    assert [(r.provider, r.model) for r in rows] == [("served-by-failover", "test-fixture-model")]


@pytest.mark.parametrize("path", ["chat", "stream", "headless", "voice"])
async def test_an_unpriced_model_never_costs_the_reply_and_is_not_stored(  # type: ignore[no-untyped-def]
    settings, session_manager, cost_tracker, tool_registry, tmp_path, caplog, path: str
) -> None:
    """The provider has billed the request by the time it is recorded: a missing price is logged, the reply kept."""
    import logging
    from dataclasses import replace

    from pincer.core.agent import Agent

    unpriced = replace(_response(_FIRST), request_model="no-such-priced-model")
    agent = Agent(settings, _scripted_llm(unpriced), session_manager, cost_tracker, tool_registry)
    with caplog.at_level(logging.ERROR, logger="pincer.services.costs"):
        if path == "chat":
            reply = (await agent.handle_message("user", "chat", "hi", memory=False)).text
        elif path == "stream":
            chunks = [c async for c in agent.handle_message_stream("user", "web", "hi", memory=False)]
            reply = chunks[-1].content
        elif path == "headless":
            reply = await agent.run_headless("hi", "user", "chat")
        else:
            chunks = [c async for c in agent.stream_voice_turn("user", "voice", "hi")]
            reply = "".join(c.content for c in chunks if c.type.value == "text")

    assert reply == "Done."
    assert await _stored(tmp_path) == []
    assert [r.getMessage() for r in caplog.records if r.name == "pincer.services.costs"] == [
        "No [pricing] entry for model 'no-such-priced-model' (provider anthropic): the call is not recorded"
    ]
