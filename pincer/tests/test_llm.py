"""Tests for LLM base types and cost tracker."""

import pytest

from pincer.llm.base import LLMMessage, MessageRole, ToolCall
from pincer.llm.cost_tracker import CostTracker, calculate_cost


def test_message_serialization() -> None:
    msg = LLMMessage(
        role=MessageRole.ASSISTANT,
        content="Hello",
        tool_calls=[ToolCall(id="tc1", name="greet", arguments={"name": "World"})],
    )
    d = msg.to_dict()
    assert d["role"] == "assistant"
    assert d["content"] == "Hello"
    assert len(d["tool_calls"]) == 1

    restored = LLMMessage.from_dict(d)
    assert restored.role == MessageRole.ASSISTANT
    assert restored.content == "Hello"
    assert restored.tool_calls[0].name == "greet"


def test_calculate_cost_known_model() -> None:
    cost = calculate_cost("gpt-4o-mini", input_tokens=1000, output_tokens=500)
    expected = (1000 * 0.15 + 500 * 0.60) / 1_000_000
    assert abs(cost - expected) < 1e-10


def test_calculate_cost_unknown_model() -> None:
    cost = calculate_cost("unknown-model", input_tokens=1000, output_tokens=500)
    expected = (1000 * 3.0 + 500 * 15.0) / 1_000_000
    assert abs(cost - expected) < 1e-10


@pytest.mark.asyncio
async def test_cost_tracker_record(cost_tracker: CostTracker) -> None:
    cost = await cost_tracker.record(
        provider="test",
        model="gpt-4o-mini",
        input_tokens=1000,
        output_tokens=500,
    )
    assert cost > 0

    today = await cost_tracker.get_today_spend()
    assert today == cost


@pytest.mark.asyncio
async def test_cost_tracker_summary(cost_tracker: CostTracker) -> None:
    await cost_tracker.record("test", "gpt-4o", 100, 50)
    await cost_tracker.record("test", "gpt-4o", 200, 100)

    summary = await cost_tracker.get_summary()
    assert summary.total_calls == 2
    assert summary.total_input_tokens == 300
    assert summary.total_output_tokens == 150
    assert summary.total_usd > 0
