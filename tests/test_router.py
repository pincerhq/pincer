"""Tests for LLMRouter — construction, resolution, failover, summary, is_free."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic_sse as sse
import httpx
import pytest

from pincer.core.log_once import LogOncePerKey
from pincer.exceptions import LLMError, LLMRateLimitError
from pincer.llm import router as router_mod
from pincer.llm.base import (
    RATE_LIMIT_ATTEMPTS,
    BaseLLMProvider,
    LLMMessage,
    LLMResponse,
    MessageRole,
    RequestClients,
    StreamTurnEvent,
    rate_limit_attempts,
    rate_limit_retries,
)
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

    async def stream_turn(self, messages=None, **kwargs):  # type: ignore[override]
        if self.stream_fail_before:
            raise LLMError(f"{self.name} stream boom")
        for token in self.stream_tokens:
            yield StreamTurnEvent(text=token)
        if self.stream_fail_after:
            raise LLMError(f"{self.name} stream mid boom")

    async def close(self) -> None:
        self.closed = True


def _router_with_pool(default, failovers, pool, *, summary_provider=None, summary_model="summary-model"):
    r = LLMRouter.__new__(LLMRouter)
    r._settings = SimpleNamespace(
        default_provider=default,
        fallback_providers=failovers,
        summary_provider=summary_provider,
        summary_model=summary_model,
        openai_compatible_provider="",
        anthropic_compatible_provider="",
    )
    r._pool = pool
    r._on_demand = {}
    r._voice_log = LogOncePerKey(router_mod.logger)
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


# ── stream_turn() failover ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_turn_fails_over_before_first_event():
    primary = FakeProvider("primary", stream_fail_before=True)
    fb = FakeProvider("fb", stream_tokens=["He", "llo"])
    r = _router_with_pool("primary", ["fb"], {"primary": primary, "fb": fb})

    tokens = [e.text async for e in r.stream_turn(messages=[])]

    assert tokens == ["He", "llo"]


@pytest.mark.asyncio
async def test_stream_turn_no_failover_after_first_event():
    primary = FakeProvider("primary", stream_tokens=["a"], stream_fail_after=True)
    fb = FakeProvider("fb", stream_tokens=["b"])
    r = _router_with_pool("primary", ["fb"], {"primary": primary, "fb": fb})

    collected: list[str] = []
    with pytest.raises(LLMError):
        async for event in r.stream_turn(messages=[]):
            collected.append(event.text)

    assert collected == ["a"]  # mid-stream error propagates; failover NOT used


@pytest.mark.asyncio
async def test_stream_turn_primary_success():
    primary = FakeProvider("primary", stream_tokens=["x", "y"])
    r = _router_with_pool("primary", [], {"primary": primary})

    tokens = [e.text async for e in r.stream_turn(messages=[])]
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

    assert r.get_summarizer() == (primary, "summary-model")


def test_get_summarizer_uses_configured_provider():
    primary = FakeProvider("primary")
    fb = FakeProvider("fb")
    r = _router_with_pool("primary", ["fb"], {"primary": primary, "fb": fb}, summary_provider="fb")

    assert r.get_summarizer() == (fb, "summary-model")


def test_get_summarizer_unbuildable_provider_raises():
    primary = FakeProvider("primary")
    r = _router_with_pool("primary", [], {"primary": primary}, summary_provider="ghost")

    with pytest.raises(ValueError, match="could not be built"):
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


# ── summary provider/model resolution (real Settings) ───────────────────────────


def test_summarizer_drops_claude_model_on_openai_default(caplog):
    from pincer.llm.openai_common import OpenAICompatibleProvider

    settings = _settings(default_provider="openai", openai_api_key="sk-oai", default_model="gpt-5-mini")
    assert settings.summary_model.startswith("claude")
    oai, ant = _patched_clients()
    with oai, ant, patch("pincer.llm.router.get_settings", return_value=settings), caplog.at_level("WARNING"):
        router = LLMRouter()
        provider, model = router.get_summarizer()

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider is router._build_pool()["openai"]
    assert model is None
    assert "Anthropic model id" in caplog.text


def test_summarizer_provider_prefix_builds_on_demand():
    from pincer.llm.anthropic_common import AnthropicCompatibleProvider

    settings = _settings(
        default_provider="openai",
        openai_api_key="sk-oai",
        default_model="gpt-5-mini",
        summary_model="anthropic:claude-haiku-4-5",
    )
    oai, ant = _patched_clients()
    with oai, ant, patch("pincer.llm.router.get_settings", return_value=settings):
        router = LLMRouter()
        assert "anthropic" not in router._build_pool()
        provider, model = router.get_summarizer()

    assert isinstance(provider, AnthropicCompatibleProvider)
    assert model == "claude-haiku-4-5"


def test_summary_provider_outside_pool_builds_on_demand():
    from pincer.llm.anthropic_common import AnthropicCompatibleProvider

    settings = _settings(
        default_provider="openai",
        openai_api_key="sk-oai",
        default_model="gpt-5-mini",
        summary_provider="anthropic",
        summary_model="claude-haiku-4-5",
    )
    oai, ant = _patched_clients()
    with oai, ant, patch("pincer.llm.router.get_settings", return_value=settings):
        provider, model = LLMRouter().get_summarizer()

    assert isinstance(provider, AnthropicCompatibleProvider)
    assert model == "claude-haiku-4-5"


def test_summary_provider_without_credentials_raises():
    settings = _settings(
        default_provider="openai",
        openai_api_key="sk-oai",
        anthropic_api_key="",
        default_model="gpt-5-mini",
        summary_provider="my-claude",
    )
    oai, ant = _patched_clients()
    with oai, ant, patch("pincer.llm.router.get_settings", return_value=settings), pytest.raises(ValueError):
        LLMRouter().get_summarizer()


def test_summary_model_with_colon_is_not_a_provider_prefix():
    settings = _settings(
        default_provider="ollama",
        openai_compatible_provider="ollama",
        openai_compatible_base_url="http://localhost:11434/v1",
        openai_compatible_model="llama3:8b",
        summary_model="llama3:8b",
    )
    oai, ant = _patched_clients()
    with oai, ant, patch("pincer.llm.router.get_settings", return_value=settings):
        router = LLMRouter()
        provider, model = router.get_summarizer()

    assert provider is router._build_pool()["ollama"]
    assert model == "llama3:8b"


def test_vendor_prefixed_claude_id_is_kept_on_openai_wire():
    settings = _settings(
        default_provider="openrouter",
        openai_compatible_provider="openrouter",
        openai_compatible_base_url="https://openrouter.ai/api/v1",
        openai_compatible_model="openai/gpt-5-mini",
        summary_model="anthropic/claude-haiku-4.5",
    )
    oai, ant = _patched_clients()
    with oai, ant, patch("pincer.llm.router.get_settings", return_value=settings):
        _, model = LLMRouter().get_summarizer()

    assert model == "anthropic/claude-haiku-4.5"


# ── Effort and thinking reach every provider (issue #345) ────────────────────


async def _call(router: LLMRouter, method: str, **kwargs) -> None:
    messages = [LLMMessage(role=MessageRole.USER, content="hi")]
    if method == "complete":
        await router.complete(messages, **kwargs)
    else:
        _ = [e async for e in getattr(router, method)(messages, **kwargs)]


@pytest.mark.parametrize("method", ["complete", "stream_turn"])
async def test_effort_and_thinking_reach_the_primary_and_the_failover(settings, method):
    """The primary fails (400), the failover answers: both requests carry the call's values."""
    async with (
        sse.ScriptedAnthropic(settings.model_copy(update={"anthropic_model": "claude-haiku-5-5"}), [400]) as primary,
        sse.ScriptedAnthropic(
            settings.model_copy(update={"anthropic_model": "claude-opus-5"}), [([sse.text("ok")], "end_turn")]
        ) as failover,
    ):
        router = _router_with_pool("a", ["b"], {"a": primary.provider, "b": failover.provider})
        await _call(router, method, reasoning_effort="low", thinking="adaptive")

    sent = [*primary.requests, *failover.requests]
    assert [body["model"] for body in sent] == ["claude-haiku-5-5", "claude-opus-5"]
    assert [body["output_config"] for body in sent] == [{"effort": "low"}] * 2
    drop_block = {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
    assert [body["thinking"] for body in sent] == [drop_block] * 2  # both are 5.x models on 'anthropic' (issue #358)


async def test_failover_off_sonnet_5_5_resolves_disabled_for_its_own_model(settings):
    """between_tools is Sonnet 5.5's form only: a failover to another model never re-sends it."""
    async with (
        sse.ScriptedAnthropic(settings.model_copy(update={"anthropic_model": "claude-sonnet-5-5"}), [400]) as primary,
        sse.ScriptedAnthropic(
            settings.model_copy(update={"anthropic_model": "claude-haiku-5-5"}), [([sse.text("ok")], "end_turn")]
        ) as failover,
    ):
        router = _router_with_pool("a", ["b"], {"a": primary.provider, "b": failover.provider})
        await _call(router, "stream_turn", reasoning_effort="low", thinking="disabled")

    assert primary.requests[0]["thinking"] == {"type": "between_tools"}  # the precondition: Sonnet 5.5 got its form
    assert failover.requests[0]["model"] == "claude-haiku-5-5"
    assert failover.requests[0]["thinking"] == {"type": "disabled"}


# ── A rate limit fails over first (PR #366 review, Q23) ──────────────────────

_RATE_LIMITED_EVERY_TIME = [429] * 9  # more than 3 SDK tries × 3 provider attempts could use
_SLEEP = "pincer.llm.anthropic_common.asyncio.sleep"


async def _answer(router: LLMRouter, method: str, **kwargs: Any) -> LLMResponse:
    """The router's answer to one `method` call; a stream's text must be its response's text."""
    messages = [LLMMessage(role=MessageRole.USER, content="hi")]
    if method == "complete":
        return await router.complete(messages, **kwargs)
    events = [e async for e in router.stream_turn(messages, **kwargs)]
    final = events[-1].response
    assert final is not None
    assert "".join(e.text for e in events[:-1]) == final.content
    return final


@pytest.mark.parametrize("method", ["stream_turn", "complete"])
async def test_a_rate_limited_primary_fails_over_at_once(settings, method):
    """With a fallback configured, the primary's 429 goes straight to the router: one HTTP
    request (no SDK retry, no provider retry), no wait, and the fallback answers."""
    with patch(_SLEEP, new_callable=AsyncMock) as sleep:
        async with (
            sse.ScriptedAnthropic(settings, _RATE_LIMITED_EVERY_TIME) as primary,
            sse.ScriptedAnthropic(settings, [([sse.text("from the fallback")], "end_turn")]) as fallback,
        ):
            router = _router_with_pool("a", ["b"], {"a": primary.provider, "b": fallback.provider})
            answer = await _answer(router, method)

    assert answer.content == "from the fallback"
    assert len(fallback.requests) == 1
    assert len(primary.requests) == 1
    sleep.assert_not_awaited()


async def _rate_limited_openai(settings) -> tuple[BaseLLMProvider, list[httpx.Request]]:
    """An OpenAI provider on the real SDK whose every request is answered 429 (asking for no wait),
    and the requests it sent."""
    from openai import AsyncOpenAI
    from pydantic import SecretStr

    from pincer.llm.openai_common import OpenAICompatibleProvider

    sent: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        error = {"error": {"type": "rate_limit_exceeded", "message": "scripted 429"}}
        return httpx.Response(429, json=error, headers={"retry-after-ms": "1", "retry-after": "0"})

    provider = OpenAICompatibleProvider(
        settings.model_copy(update={"openai_api_key": SecretStr("sk-test"), "openai_model": "gpt-5-mini"}), "openai"
    )
    await provider.close()
    provider._client = AsyncOpenAI(  # type: ignore[attr-defined]
        api_key="sk-test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    )
    return provider, sent


@pytest.mark.parametrize("method", ["stream_turn", "complete"])
async def test_a_rate_limited_openai_primary_fails_over_at_once(settings, method):
    primary, sent = await _rate_limited_openai(settings)
    with patch("pincer.llm.openai_common.asyncio.sleep", new_callable=AsyncMock) as sleep:
        async with sse.ScriptedAnthropic(settings, [([sse.text("from the fallback")], "end_turn")]) as fallback:
            router = _router_with_pool("a", ["b"], {"a": primary, "b": fallback.provider})
            try:
                answer = await _answer(router, method)
            finally:
                await primary.close()

    assert answer.content == "from the fallback"
    assert len(sent) == 1
    sleep.assert_not_awaited()


@pytest.mark.parametrize("method", ["stream_turn", "complete"])
async def test_a_lone_primary_keeps_its_rate_limit_retry(settings, method):
    """With no failover, the primary is tried once, with the retry it always had: 3 attempts of 3
    SDK tries, two waits (retry-after 0 doubles to 0.0), then the rate limit is raised."""
    with patch(_SLEEP, new_callable=AsyncMock) as sleep:
        async with sse.ScriptedAnthropic(settings, [*_RATE_LIMITED_EVERY_TIME, 429]) as primary:
            router = _router_with_pool("a", [], {"a": primary.provider})
            with pytest.raises(LLMRateLimitError):
                await _answer(router, method)

    assert len(primary.requests) == 9
    assert [c.args for c in sleep.await_args_list] == [(0.0,), (0.0,)]


class _RateLimitedRecorder(BaseLLMProvider):
    """Raises a 429 on every call, after noting its name and how many attempts it was allowed."""

    def __init__(self, name: str, seen: list[tuple[str, int]]) -> None:
        self._name = name
        self._seen = seen

    async def complete(self, messages=None, **kwargs):  # type: ignore[override]
        self._seen.append((self._name, rate_limit_attempts()))
        raise LLMRateLimitError(retry_after=1.0)

    async def stream_turn(self, messages=None, **kwargs):  # type: ignore[override]
        self._seen.append((self._name, rate_limit_attempts()))
        raise LLMRateLimitError(retry_after=1.0)
        yield StreamTurnEvent()  # pragma: no cover - makes this an async generator

    async def close(self) -> None:
        pass


@pytest.mark.parametrize("method", ["stream_turn", "complete"])
async def test_only_the_primarys_last_try_retries(method):
    """Q24: the primary and each failover get one attempt; then the primary again, with its retry."""
    seen: list[tuple[str, int]] = []
    pool = {name: _RateLimitedRecorder(name, seen) for name in ("a", "b", "c")}
    router = _router_with_pool("a", ["b", "c"], pool)

    with pytest.raises(LLMRateLimitError):
        await _answer(router, method)

    assert [seen[0], seen[-1]] == [("a", 1), ("a", RATE_LIMIT_ATTEMPTS)]
    assert sorted(seen[1:-1]) == [("b", 1), ("c", 1)]


async def test_the_retry_setting_never_reaches_a_streams_consumer():
    """The primary (not last) streams: while the consumer handles each event, and after, its
    own requests still retry a 429."""
    primary = FakeProvider("primary", stream_tokens=["He", "llo"])
    router = _router_with_pool("primary", ["fb"], {"primary": primary, "fb": FakeProvider("fb")})

    seen: list[tuple[str, int]] = []
    async for event in router.stream_turn(messages=[]):
        seen.append((event.text, rate_limit_attempts()))

    assert seen == [("He", RATE_LIMIT_ATTEMPTS), ("llo", RATE_LIMIT_ATTEMPTS)]
    assert rate_limit_attempts() == RATE_LIMIT_ATTEMPTS


async def test_a_concurrent_turn_keeps_its_rate_limit_retry():
    """Turn A's primary waits inside its first request with retries off; turn B, running
    meanwhile, still retries. Then A fails over."""
    import asyncio

    inside, release = asyncio.Event(), asyncio.Event()
    seen_by_a: list[int] = []

    class _Blocking(FakeProvider):
        async def stream_turn(self, messages=None, **kwargs):  # type: ignore[override]
            seen_by_a.append(rate_limit_attempts())
            inside.set()
            await release.wait()
            raise LLMRateLimitError(retry_after=1.0)
            yield StreamTurnEvent()  # pragma: no cover - makes this an async generator

    router = _router_with_pool("a", ["b"], {"a": _Blocking("a"), "b": FakeProvider("b", stream_tokens=["from b"])})

    async def turn_a() -> list[str]:
        return [e.text async for e in router.stream_turn(messages=[])]

    async def turn_b() -> int:
        await inside.wait()
        attempts = rate_limit_attempts()
        release.set()
        return attempts

    tokens, seen_by_b = await asyncio.gather(turn_a(), turn_b())

    assert seen_by_a == [1]  # the precondition: A's request really ran with retries off
    assert seen_by_b == RATE_LIMIT_ATTEMPTS
    assert tokens == ["from b"]


@pytest.mark.parametrize("method", ["stream_turn", "complete"])
async def test_a_transient_primary_rate_limit_recovers_when_every_fallback_fails(settings, method):
    """Q24: the primary's 429 fails over at once; the fallback is broken (401), so the primary is
    asked again, with its retry, and answers. The call's model goes to the primary both times."""
    replies = [429, ([sse.text("from the primary's retry")], "end_turn")]
    with patch(_SLEEP, new_callable=AsyncMock) as sleep:
        async with (
            sse.ScriptedAnthropic(settings, replies) as primary,
            sse.ScriptedAnthropic(settings, [401]) as fallback,
        ):
            router = _router_with_pool("a", ["b"], {"a": primary.provider, "b": fallback.provider})
            answer = await _answer(router, method, model="claude-explicit-1")

    assert answer.content == "from the primary's retry"
    assert [body["model"] for body in primary.requests] == ["claude-explicit-1"] * 2
    assert [body["model"] for body in fallback.requests] == [settings.anthropic_model or settings.default_model]
    sleep.assert_not_awaited()


@pytest.mark.parametrize("method", ["stream_turn", "complete"])
async def test_when_the_primarys_retry_fails_too_its_last_error_is_raised(settings, method):
    """The primary's first try is overloaded (529), the fallback is broken, and the primary's
    retry stays rate limited: its rate limit is raised, not the first try's 529."""
    with patch(_SLEEP, new_callable=AsyncMock) as sleep:
        async with (
            sse.ScriptedAnthropic(settings, [529, *_RATE_LIMITED_EVERY_TIME]) as primary,
            sse.ScriptedAnthropic(settings, [401]) as fallback,
        ):
            router = _router_with_pool("a", ["b"], {"a": primary.provider, "b": fallback.provider})
            with pytest.raises(LLMRateLimitError):
                await _answer(router, method)

    assert len(primary.requests) == 1 + 9  # the first try, then 3 attempts of 3 SDK tries
    assert len(fallback.requests) == 1
    assert [c.args for c in sleep.await_args_list] == [(0.0,), (0.0,)]


async def test_one_no_retry_copy_per_sdk_client():
    """Requests that fail over at once share one copy of their client without SDK retries; a
    swapped-in client (as the scripted tests do) gets its own."""
    from anthropic import AsyncAnthropic

    clients = RequestClients[AsyncAnthropic]()
    client, other = AsyncAnthropic(api_key="sk-ant-test"), AsyncAnthropic(api_key="sk-ant-test")
    try:
        assert clients.pick(client) is client  # retries on: the client itself
        with rate_limit_retries(False):
            copy = clients.pick(client)
            assert copy is not client
            assert copy.max_retries == 0
            assert clients.pick(client) is copy
            assert clients.pick(other) is not copy
            assert clients.pick(other).max_retries == 0
    finally:
        await client.close()
        await other.close()


class _PlainClient:
    """An SDK client double that can be neither hashed nor weakly referenced."""

    __slots__ = ("max_retries",)
    __hash__ = None  # type: ignore[assignment]

    def __init__(self, max_retries: int = 2) -> None:
        self.max_retries = max_retries

    def with_options(self, *, max_retries: int) -> _PlainClient:
        return _PlainClient(max_retries)


def test_a_client_that_cannot_be_hashed_or_weakly_referenced_gets_its_copy():
    clients = RequestClients[_PlainClient]()
    client = _PlainClient()
    with rate_limit_retries(False):
        copy = clients.pick(client)
        assert copy.max_retries == 0
        assert clients.pick(client) is copy


@pytest.mark.parametrize("method", ["stream_turn", "complete"])
async def test_a_primary_that_failed_for_good_is_not_tried_again(settings, method):
    """Q24a: a 401 is not transient, so when the fallback fails too the primary's own error is
    raised at once; asking the primary again would only repeat it."""
    async with (
        sse.ScriptedAnthropic(settings, [401, ([sse.text("never sent")], "end_turn")]) as primary,
        sse.ScriptedAnthropic(settings, [404]) as fallback,
    ):
        router = _router_with_pool("a", ["b"], {"a": primary.provider, "b": fallback.provider})
        with pytest.raises(LLMError, match="401"):
            await _answer(router, method)

    assert len(primary.requests) == 1
    assert len(fallback.requests) == 1


@pytest.mark.parametrize("method", ["stream_turn", "complete"])
async def test_a_primary_that_timed_out_is_not_tried_again(settings, method):
    """Q24a: a timeout is not transient: on a call, asking again would only add dead air."""
    replies = [httpx.ReadTimeout("scripted timeout"), ([sse.text("never sent")], "end_turn")]
    async with (
        sse.ScriptedAnthropic(settings, replies) as primary,
        sse.ScriptedAnthropic(settings, [401]) as fallback,
    ):
        router = _router_with_pool("a", ["b"], {"a": primary.provider, "b": fallback.provider})
        with pytest.raises(LLMError, match="Anthropic connection error"):
            await _answer(router, method)

    assert len(primary.requests) == 1
    assert len(fallback.requests) == 1


async def _openai_answering(settings, statuses: list[int], text: str) -> tuple[BaseLLMProvider, list[httpx.Request]]:
    """An OpenAI provider on the real SDK answering each status in `statuses` in turn, then `text`
    (as a stream or a completion, whichever was asked), and the requests it sent."""
    import json

    from openai import AsyncOpenAI
    from pydantic import SecretStr

    from pincer.llm.openai_common import OpenAICompatibleProvider

    pending = list(statuses)
    sent: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if pending:
            return httpx.Response(pending.pop(0), json={"error": {"type": "server_error", "message": "scripted"}})
        message = {"role": "assistant", "content": text}
        if json.loads(request.content).get("stream"):
            chunk = {
                "id": "c1",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "gpt-5-mini",
                "choices": [{"index": 0, "delta": message, "finish_reason": "stop"}],
            }
            body = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n"
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())
        completion = {
            "id": "c1",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-5-mini",
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        return httpx.Response(200, json=completion)

    provider = OpenAICompatibleProvider(
        settings.model_copy(update={"openai_api_key": SecretStr("sk-test"), "openai_model": "gpt-5-mini"}), "openai"
    )
    await provider.close()
    provider._client = AsyncOpenAI(  # type: ignore[attr-defined]
        api_key="sk-test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    )
    return provider, sent


@pytest.mark.parametrize("method", ["stream_turn", "complete"])
async def test_an_openai_primary_5xx_earns_its_last_try(settings, method):
    """Q24a: a server error is transient, so with the fallback broken the primary is asked again."""
    primary, sent = await _openai_answering(settings, [503], "from the primary's retry")
    try:
        async with sse.ScriptedAnthropic(settings, [401]) as fallback:
            router = _router_with_pool("a", ["b"], {"a": primary, "b": fallback.provider})
            answer = await _answer(router, method)
    finally:
        await primary.close()

    assert answer.content == "from the primary's retry"
    assert len(sent) == 2


_LOOKUP_TOOL = {"name": "lookup", "description": "Look a word up.", "input_schema": {"type": "object"}}


@pytest.mark.parametrize("method", ["stream_turn", "complete"])
async def test_every_try_sends_the_same_request(settings, method):
    """The primary's first try, the fallback and the primary's retry all send the call's system,
    max_tokens and tools: only the model may differ."""
    async with (
        sse.ScriptedAnthropic(settings, [429, ([sse.text("ok")], "end_turn")]) as primary,
        sse.ScriptedAnthropic(settings, [401]) as fallback,
    ):
        router = _router_with_pool("a", ["b"], {"a": primary.provider, "b": fallback.provider})
        await _answer(router, method, system="S", max_tokens=123, tools=[_LOOKUP_TOOL])

    sent = [primary.requests[0], fallback.requests[0], primary.requests[1]]
    assert [body["max_tokens"] for body in sent] == [123] * 3
    assert len({json.dumps([body["system"], body["tools"]], sort_keys=True) for body in sent}) == 1
    assert [tool["name"] for tool in sent[0]["tools"]] == ["lookup"]  # the precondition: tools were sent
    assert "S" in json.dumps(sent[0]["system"])
