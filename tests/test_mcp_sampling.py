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


def _settings(tmp_path, budget: float = 0.0, **overrides):  # type: ignore[no-untyped-def]
    """Explicit settings (no developer .env): one Anthropic key, the cost log in tmp_path."""
    from pincer.config import Settings

    base = {
        "telegram_bot_token": "123456:TEST",
        "anthropic_api_key": "sk-ant-test",
        "data_dir": tmp_path,
        "daily_budget_usd": budget,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def _shell(model: str = "test-fixture-model"):  # type: ignore[no-untyped-def]
    from pincer.mcp.config import MCPConfig, MCPSamplingConfig, MCPServerExportConfig
    from pincer.mcp.standalone import StandaloneMCPShell

    sampling = MCPSamplingConfig(enabled=True, model=model)
    return StandaloneMCPShell(mcp_config=MCPConfig(server=MCPServerExportConfig(enabled=True, sampling=sampling)))


class _Anthropic:
    """The Anthropic API as the real SDK sees it: every request recorded, each answered
    with one streamed text reply of 1,000 input tokens."""

    def __init__(self) -> None:
        self.requests: list[dict] = []

    def client(self, **kwargs):  # type: ignore[no-untyped-def]
        import httpx
        from anthropic import AsyncAnthropic

        return AsyncAnthropic(
            api_key=kwargs.get("api_key"), http_client=httpx.AsyncClient(transport=httpx.MockTransport(self._handle))
        )

    def _handle(self, request):  # type: ignore[no-untyped-def]
        import json

        import httpx
        from anthropic_sse import sse_body, text

        self.requests.append(json.loads(request.content))
        body = sse_body([text("sampled")], "end_turn", usage={"input_tokens": 1_000})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)


@pytest.fixture
def anthropic_api(monkeypatch):  # type: ignore[no-untyped-def]
    api = _Anthropic()
    monkeypatch.setattr("pincer.llm.anthropic_common.AsyncAnthropic", api.client)
    return api


@pytest.fixture
def use_settings(monkeypatch):  # type: ignore[no-untyped-def]
    def use(settings):  # type: ignore[no-untyped-def]
        monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: settings)
        return settings

    return use


async def _sample(shell, model: str | None = None) -> str:  # type: ignore[no-untyped-def]
    backend = await shell._build_llm_backend()
    handler = SamplingHandler(llm_backend=backend, enabled=True)
    return await handler.handle([{"role": "user", "content": "hi"}], model=model)


async def _stored(settings) -> list[tuple[str, float]]:  # type: ignore[no-untyped-def]
    from pincer.config.pricing import load_price_table
    from pincer.llm.cost_tracker import CostTracker

    tracker = CostTracker(settings.db_path, prices=load_price_table())
    await tracker.initialize()
    rows = await tracker.get_costs_by_model("2000-01-01", "2100-01-01")
    return [(r["model"], r["total"]) for r in rows]


async def test_a_standalone_sampling_request_is_recorded(tmp_path, anthropic_api, use_settings):  # type: ignore[no-untyped-def]
    """Through the shell's own backend: the provider's usage, priced and stored (5 output tokens from
    the stream). Sent with the pinned effort and thinking of an internal call, not the operator's."""
    settings = use_settings(_settings(tmp_path, reasoning_effort="high", thinking="adaptive"))

    reply = await _sample(_shell("claude-opus-4-7"))

    assert reply == "sampled"
    assert [(r["model"], r.get("output_config"), r.get("thinking")) for r in anthropic_api.requests] == [
        ("claude-opus-4-7", {"effort": "low"}, None)
    ]
    assert await _stored(settings) == [("claude-opus-4-7", pytest.approx((1_000 * 1 + 5 * 2) / 1_000_000))]


async def test_with_the_budget_reached_standalone_sampling_sends_nothing(tmp_path, anthropic_api, use_settings):  # type: ignore[no-untyped-def]
    from pincer.config.pricing import TokenUsage, load_price_table
    from pincer.llm.cost_tracker import CostTracker

    settings = use_settings(_settings(tmp_path, budget=0.007))
    seed = CostTracker(settings.db_path, prices=load_price_table())
    await seed.initialize()
    await seed.record("anthropic", "test-fixture-model", TokenUsage(input_tokens=1_000))

    reply = await _sample(_shell())

    assert reply == "[Sampling: daily budget reached]"
    assert anthropic_api.requests == []


async def test_without_an_anthropic_key_sampling_has_no_backend(tmp_path, anthropic_api, use_settings, caplog):  # type: ignore[no-untyped-def]
    from pincer.config.main import _RelaxedSettings

    use_settings(_RelaxedSettings(_env_file=None, data_dir=tmp_path, anthropic_api_key=""))  # type: ignore[call-arg]

    reply = await _sample(_shell())

    assert reply == "[Sampling: no LLM backend configured]"
    assert anthropic_api.requests == []
    assert [r.getMessage() for r in caplog.records if r.levelname == "ERROR"] == [
        "MCP sampling is enabled but has no Anthropic API key (PINCER_ANTHROPIC_API_KEY)"
    ]


def test_sampling_defaults_to_a_shipped_model() -> None:
    """The retired claude-sonnet-4 default had no price, so its spend could never be recorded."""
    from pincer.config.pricing import load_price_table
    from pincer.mcp.config import MCPSamplingConfig
    from pincer.mcp.sampling import DEFAULT_SAMPLING_MODEL

    assert MCPSamplingConfig().model == "claude-sonnet-5-5"
    assert DEFAULT_SAMPLING_MODEL == "claude-sonnet-5-5"
    repo_root = __import__("pathlib").Path(__file__).resolve().parents[1]
    assert "claude-sonnet-5-5" in load_price_table(repo_root)
