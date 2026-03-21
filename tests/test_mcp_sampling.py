"""Tests for MCP sampling handler."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pincer.mcp.sampling import (
    AgentLLMBackend,
    SamplingHandler,
    StandaloneLLMBackend,
    _DailyBudgetTracker,
)


# ── _DailyBudgetTracker ───────────────────────────────────────────────────────


def test_budget_tracker_initial():
    tracker = _DailyBudgetTracker(budget=5.0)
    assert tracker.remaining() == pytest.approx(5.0)
    assert not tracker.exhausted


def test_budget_tracker_record_and_remaining():
    tracker = _DailyBudgetTracker(budget=5.0)
    tracker.record(1.0)
    assert tracker.remaining() == pytest.approx(4.0)


def test_budget_tracker_exhausted():
    tracker = _DailyBudgetTracker(budget=1.0)
    tracker.record(1.5)
    assert tracker.exhausted
    assert tracker.remaining() == 0.0


def test_budget_tracker_reset_on_new_day():
    tracker = _DailyBudgetTracker(budget=5.0)
    tracker.record(3.0)
    # Force a "new day" by manipulating the internal day counter
    tracker._day = -1
    # remaining() triggers reset
    assert tracker.remaining() == pytest.approx(5.0)


# ── SamplingHandler ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sampling_disabled_by_default():
    handler = SamplingHandler()
    result = await handler.handle([{"role": "user", "content": "hello"}])
    assert "disabled" in result.lower()


@pytest.mark.asyncio
async def test_sampling_no_backend():
    handler = SamplingHandler(enabled=True, llm_backend=None)
    result = await handler.handle([])
    assert "no llm backend" in result.lower()


@pytest.mark.asyncio
async def test_sampling_routes_to_llm():
    mock_backend = AsyncMock()
    mock_backend.complete = AsyncMock(return_value="The answer is 42.")

    handler = SamplingHandler(
        llm_backend=mock_backend,
        enabled=True,
        budget_per_day=10.0,
    )
    result = await handler.handle([{"role": "user", "content": "What is the answer?"}])
    assert result == "The answer is 42."
    mock_backend.complete.assert_called_once()


@pytest.mark.asyncio
async def test_sampling_respects_budget():
    mock_backend = AsyncMock()
    mock_backend.complete = AsyncMock(return_value="x" * 100)

    handler = SamplingHandler(
        llm_backend=mock_backend,
        enabled=True,
        budget_per_day=0.0,  # zero budget → exhausted immediately
    )
    result = await handler.handle([])
    assert "budget exhausted" in result.lower()
    mock_backend.complete.assert_not_called()


@pytest.mark.asyncio
async def test_sampling_tracks_spend():
    mock_backend = AsyncMock()
    mock_backend.complete = AsyncMock(return_value="x" * 40000)  # many chars

    handler = SamplingHandler(
        llm_backend=mock_backend,
        enabled=True,
        budget_per_day=100.0,
        cost_per_token=0.01,
    )
    initial_budget = handler.budget_remaining
    await handler.handle([])
    # Budget should decrease after a call
    assert handler.budget_remaining < initial_budget


@pytest.mark.asyncio
async def test_sampling_llm_error_returns_error_string():
    mock_backend = AsyncMock()
    mock_backend.complete = AsyncMock(side_effect=RuntimeError("LLM failure"))

    handler = SamplingHandler(
        llm_backend=mock_backend,
        enabled=True,
        budget_per_day=10.0,
    )
    result = await handler.handle([])
    assert "error" in result.lower()


def test_sampling_register_on_fmcp_no_hook():
    """register_on is a no-op if FastMCP has no sampling hook."""
    handler = SamplingHandler(enabled=False)
    fmcp = MagicMock(spec=[])  # no set_sampling_callback
    handler.register_on(fmcp)  # should not raise


def test_sampling_register_on_fmcp_with_hook():
    """register_on wires the callback if the hook is available."""
    handler = SamplingHandler(enabled=False)
    fmcp = MagicMock()
    handler.register_on(fmcp)
    fmcp.set_sampling_callback.assert_called_once()


# ── AgentLLMBackend ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_llm_backend_complete():
    mock_router = AsyncMock()
    mock_router.complete = AsyncMock(return_value="agent response")

    backend = AgentLLMBackend(llm_router=mock_router)
    result = await backend.complete([{"role": "user", "content": "hi"}])
    assert result == "agent response"


@pytest.mark.asyncio
async def test_agent_llm_backend_fallback_to_chat():
    """Fallback to .chat() if .complete() raises AttributeError."""
    class FakeRouter:
        async def chat(self, messages):
            return "chat response"

    backend = AgentLLMBackend(llm_router=FakeRouter())
    result = await backend.complete([])
    assert result == "chat response"
