"""Tests for LLMRouter — construction, resolution, failover, summary, is_free."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from pincer.exceptions import LLMError
from pincer.llm.base import BaseLLMProvider, LLMResponse
from pincer.llm.router import LLMRouter


class FakeProvider(BaseLLMProvider):
    """Configurable in-memory provider for exercising router logic."""

    def __init__(
        self,
        name: str,
        *,
        complete_fail: bool = False,
        stream_tokens: list[str] | None = None,
        stream_fail_before: bool = False,
        stream_fail_after: bool = False,
    ) -> None:
        self.name = name
        self.complete_fail = complete_fail
        self.stream_tokens = stream_tokens or []
        self.stream_fail_before = stream_fail_before
        self.stream_fail_after = stream_fail_after
        self.complete_calls = 0
        self.closed = False

    async def complete(self, messages=None, **kwargs):  # type: ignore[override]
        self.complete_calls += 1
        if self.complete_fail:
            raise LLMError(f"{self.name} boom")
        return LLMResponse(content=f"from {self.name}", model=self.name, provider=self.name)

    async def stream(self, messages=None, **kwargs):  # type: ignore[override]
        if self.stream_fail_before:
            raise LLMError(f"{self.name} stream boom")
        for token in self.stream_tokens:
            yield token
        if self.stream_fail_after:
            raise LLMError(f"{self.name} stream mid boom")

    async def close(self) -> None:
        self.closed = True


def _router_with_pool(default, failovers, pool, *, summary_provider=None):
    r = LLMRouter.__new__(LLMRouter)
    r._settings = SimpleNamespace(
        default_provider=default,
        fallback_providers=failovers,
        summary_provider=summary_provider,
    )
    r._pool = pool
    return r


# ── complete() failover ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_complete_primary_success_no_failover():
    primary = FakeProvider("primary")
    fb = FakeProvider("fb")
    r = _router_with_pool("primary", ["fb"], {"primary": primary, "fb": fb})

    result = await r.complete(messages=[])

    assert result.content == "from primary"
    assert fb.complete_calls == 0


@pytest.mark.asyncio
async def test_complete_fails_over_and_reports_serving_provider():
    primary = FakeProvider("primary", complete_fail=True)
    fb = FakeProvider("fb")
    r = _router_with_pool("primary", ["fb"], {"primary": primary, "fb": fb})

    result = await r.complete(messages=[])

    assert result.model == "fb"  # LLMResponse.model reflects the serving provider
    assert result.provider == "fb"  # and so does .provider (used for cost attribution)


@pytest.mark.asyncio
async def test_complete_all_fail_reraises_primary_error():
    primary = FakeProvider("primary", complete_fail=True)
    fb1 = FakeProvider("fb1", complete_fail=True)
    fb2 = FakeProvider("fb2", complete_fail=True)
    r = _router_with_pool("primary", ["fb1", "fb2"], {"primary": primary, "fb1": fb1, "fb2": fb2})

    with pytest.raises(LLMError, match="primary boom"):
        await r.complete(messages=[])


@pytest.mark.asyncio
async def test_complete_second_failover_serves_when_first_fails():
    primary = FakeProvider("primary", complete_fail=True)
    fb1 = FakeProvider("fb1", complete_fail=True)
    fb2 = FakeProvider("fb2")
    r = _router_with_pool("primary", ["fb1", "fb2"], {"primary": primary, "fb1": fb1, "fb2": fb2})

    # Deterministic order so fb1 is tried first (and fails), then fb2 serves.
    with patch("pincer.llm.router.random.sample", side_effect=lambda seq, n: list(seq)):
        result = await r.complete(messages=[])

    assert result.model == "fb2"


# ── stream() failover ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_fails_over_before_first_token():
    primary = FakeProvider("primary", stream_fail_before=True)
    fb = FakeProvider("fb", stream_tokens=["He", "llo"])
    r = _router_with_pool("primary", ["fb"], {"primary": primary, "fb": fb})

    tokens = [t async for t in r.stream(messages=[])]

    assert tokens == ["He", "llo"]


@pytest.mark.asyncio
async def test_stream_no_failover_after_first_token():
    primary = FakeProvider("primary", stream_tokens=["a"], stream_fail_after=True)
    fb = FakeProvider("fb", stream_tokens=["b"])
    r = _router_with_pool("primary", ["fb"], {"primary": primary, "fb": fb})

    collected: list[str] = []
    with pytest.raises(LLMError):
        async for token in r.stream(messages=[]):
            collected.append(token)

    assert collected == ["a"]  # mid-stream error propagates; failover NOT used


@pytest.mark.asyncio
async def test_stream_primary_success():
    primary = FakeProvider("primary", stream_tokens=["x", "y"])
    r = _router_with_pool("primary", [], {"primary": primary})

    tokens = [t async for t in r.stream(messages=[])]
    assert tokens == ["x", "y"]


# ── close ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_closes_every_leaf():
    primary = FakeProvider("primary")
    fb = FakeProvider("fb")
    r = _router_with_pool("primary", ["fb"], {"primary": primary, "fb": fb})

    await r.close()

    assert primary.closed and fb.closed


# ── summary selection (§4) ──────────────────────────────────────────────────────


def test_get_summarizer_defaults_to_primary():
    primary = FakeProvider("primary")
    fb = FakeProvider("fb")
    r = _router_with_pool("primary", ["fb"], {"primary": primary, "fb": fb})

    assert r.get_summarizer() is primary


def test_get_summarizer_uses_configured_provider():
    primary = FakeProvider("primary")
    fb = FakeProvider("fb")
    r = _router_with_pool("primary", ["fb"], {"primary": primary, "fb": fb}, summary_provider="fb")

    assert r.get_summarizer() is fb


def test_get_summarizer_not_in_pool_raises():
    primary = FakeProvider("primary")
    r = _router_with_pool("primary", [], {"primary": primary}, summary_provider="ghost")

    with pytest.raises(ValueError, match="not in active pool"):
        r.get_summarizer()


# ── resolution / validation (real Settings, mocked clients) ─────────────────────


def _settings(**overrides):
    from pincer.config import Settings

    base = {
        "anthropic_api_key": "sk-ant",
        "telegram_bot_token": "123456:TEST",
        "default_provider": "anthropic",
        # Hermetic: ignore the developer's .env so compatible base URLs etc. don't leak in.
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _patched_clients():
    return (
        patch("pincer.llm.openai_common.AsyncOpenAI", MagicMock()),
        patch("pincer.llm.anthropic_common.AsyncAnthropic", MagicMock()),
    )


def test_pool_resolves_well_known_and_both_compatible():
    from pincer.llm.anthropic_common import AnthropicCompatibleProvider
    from pincer.llm.openai_common import OpenAICompatibleProvider

    settings = _settings(
        default_provider="anthropic",
        fallback_providers=["grok", "my-claude"],
        openai_compatible_provider="grok",
        openai_compatible_base_url="https://api.x.ai/v1",
        openai_compatible_model="grok-3",  # OpenAI-wire can't use the claude default
        anthropic_compatible_provider="my-claude",
        anthropic_compatible_base_url="https://proxy/v1",
    )
    oai, ant = _patched_clients()
    with oai, ant, patch("pincer.llm.router.get_settings", return_value=settings):
        router = LLMRouter()
        router.get_llm()
        pool = router._build_pool()

    assert isinstance(pool["anthropic"], AnthropicCompatibleProvider)
    assert isinstance(pool["grok"], OpenAICompatibleProvider)
    assert isinstance(pool["my-claude"], AnthropicCompatibleProvider)


def test_pool_leaves_carry_their_own_models():
    """Primary uses default_model; an Ollama-style failover uses its own model."""
    settings = _settings(
        default_provider="anthropic",
        default_model="claude-sonnet-4-5-20250929",
        fallback_providers=["ollama"],
        openai_compatible_provider="ollama",
        openai_compatible_base_url="http://localhost:11434/v1",
        openai_compatible_model="llama3.2",
    )
    oai, ant = _patched_clients()
    with oai, ant, patch("pincer.llm.router.get_settings", return_value=settings):
        router = LLMRouter()
        pool = router._build_pool()

    assert pool["anthropic"]._default_model == "claude-sonnet-4-5-20250929"
    assert pool["ollama"]._default_model == "llama3.2"


def test_pool_rejects_too_many_failovers():
    settings = _settings(fallback_providers=["a", "b", "c", "d"])
    with (
        patch("pincer.llm.router.get_settings", return_value=settings),
        pytest.raises(ValueError, match="At most 3 failover"),
    ):
        LLMRouter().get_llm()


def test_unknown_provider_raises():
    settings = _settings(default_provider="mystery")
    oai, ant = _patched_clients()
    with (
        oai,
        ant,
        patch("pincer.llm.router.get_settings", return_value=settings),
        pytest.raises(ValueError, match="unknown provider"),
    ):
        LLMRouter().get_llm()


def test_compatible_provider_collision_raises():
    settings = _settings(
        openai_compatible_provider="openai",
        openai_compatible_base_url="https://x/v1",
    )
    with patch("pincer.llm.router.get_settings", return_value=settings), pytest.raises(ValueError, match="collides"):
        LLMRouter().get_llm()


def test_compatible_missing_base_url_raises():
    settings = _settings(
        default_provider="grok",
        openai_compatible_provider="grok",
        # no base_url configured
    )
    oai, ant = _patched_clients()
    with (
        oai,
        ant,
        patch("pincer.llm.router.get_settings", return_value=settings),
        pytest.raises(ValueError, match="PINCER_OPENAI_COMPATIBLE_BASE_URL"),
    ):
        LLMRouter().get_llm()


# ── is_free ─────────────────────────────────────────────────────────────────────


def test_is_free():
    settings = _settings(
        openai_compatible_provider="ollama",
        openai_compatible_base_url="http://localhost:11434/v1",
        anthropic_compatible_provider="paid-proxy",
        anthropic_compatible_base_url="https://proxy/v1",
        anthropic_compatible_api_key="key",
    )
    with patch("pincer.llm.router.get_settings", return_value=settings):
        router = LLMRouter()

    assert router.is_free("openai") is False
    assert router.is_free("anthropic") is False
    assert router.is_free("ollama") is True  # compatible endpoint, no key
    assert router.is_free("paid-proxy") is False  # compatible endpoint, has key
    assert router.is_free("unknown") is False
