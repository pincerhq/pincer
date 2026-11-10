"""Tests for MCP sampling handler."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pincer.mcp.sampling import (
    AgentLLMBackend,
    SamplingHandler,
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


# ── the daily budget and the cost log (issue #357, PR #366 review) ───────────


class _FakeAnthropic:
    """Stands in for `anthropic.AsyncAnthropic`: one answer, with a real `Usage`."""

    created: list[dict] = []

    def __init__(self, api_key: str = "") -> None:
        from types import SimpleNamespace

        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):  # type: ignore[no-untyped-def]
        from types import SimpleNamespace

        from anthropic.types import Usage

        _FakeAnthropic.created.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(text="sampled")], usage=Usage(input_tokens=1_000, output_tokens=0)
        )


@pytest.fixture
def fake_anthropic(monkeypatch):  # type: ignore[no-untyped-def]
    import anthropic

    _FakeAnthropic.created = []
    monkeypatch.setattr(anthropic, "AsyncAnthropic", _FakeAnthropic)
    return _FakeAnthropic


async def _tracker(tmp_path, budget: float):  # type: ignore[no-untyped-def]
    from pincer.config.pricing import load_price_table
    from pincer.llm.cost_tracker import CostTracker

    return CostTracker(tmp_path / "test.db", daily_budget=budget, prices=load_price_table())


async def test_a_standalone_sampling_request_is_recorded(tmp_path, fake_anthropic):  # type: ignore[no-untyped-def]
    from pincer.mcp.sampling import StandaloneLLMBackend

    tracker = await _tracker(tmp_path, budget=0.0)
    backend = StandaloneLLMBackend(api_key="sk-test", default_model="test-fixture-model", costs=tracker)
    try:
        reply = await SamplingHandler(llm_backend=backend, enabled=True).handle([{"role": "user", "content": "hi"}])
        rows = await tracker.get_costs_by_model("2000-01-01", "2100-01-01")
    finally:
        await tracker.close()

    assert reply == "sampled"
    assert [(r["model"], r["total"]) for r in rows] == [("test-fixture-model", pytest.approx(0.007))]


async def test_with_the_budget_reached_standalone_sampling_sends_nothing(tmp_path, fake_anthropic):  # type: ignore[no-untyped-def]
    from pincer.config.pricing import TokenUsage
    from pincer.mcp.sampling import StandaloneLLMBackend

    tracker = await _tracker(tmp_path, budget=0.007)
    await tracker.initialize()
    await tracker.record("anthropic", "test-fixture-model", TokenUsage(input_tokens=1_000))
    backend = StandaloneLLMBackend(api_key="sk-test", default_model="test-fixture-model", costs=tracker)
    try:
        reply = await SamplingHandler(llm_backend=backend, enabled=True).handle([{"role": "user", "content": "hi"}])
    finally:
        await tracker.close()

    assert reply == "[Sampling: daily budget reached]"
    assert fake_anthropic.created == []


def test_sampling_defaults_to_a_shipped_model() -> None:
    """The retired claude-sonnet-4 default had no price, so its spend could never be recorded."""
    from pincer.config.pricing import load_price_table
    from pincer.mcp.config import MCPSamplingConfig
    from pincer.mcp.sampling import StandaloneLLMBackend

    assert MCPSamplingConfig().model == "claude-sonnet-5-5"
    assert StandaloneLLMBackend(api_key="sk-test")._default_model == "claude-sonnet-5-5"
    repo_root = __import__("pathlib").Path(__file__).resolve().parents[1]
    assert "claude-sonnet-5-5" in load_price_table(repo_root)
