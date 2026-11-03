"""Voice turn target resolution: which provider/model a voice turn really runs on (#305)."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from pincer.config import Settings
from pincer.llm import router as router_mod
from pincer.llm.anthropic_common import AnthropicCompatibleProvider
from pincer.llm.openai_common import OpenAICompatibleProvider
from pincer.llm.router import (
    LLMRouter,
    effective_voice_turn,
    voice_turn_target,
    voice_turn_unusable_reason,
)

AZURE_URL = "https://res.openai.azure.com/openai/v1/"


def _settings(**overrides) -> Settings:
    base = {"telegram_bot_token": "123456:TEST", "anthropic_api_key": "", "openai_api_key": ""}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _router(settings: Settings) -> LLMRouter:
    with patch("pincer.llm.router.get_settings", return_value=settings):
        return LLMRouter()


def _gemma(**overrides) -> Settings:
    """A custom Anthropic-wire default provider, no OpenAI key (the #305 deployment)."""
    return _settings(
        default_provider="gemma_anthropic",
        default_model="claude-sonnet-5-5",
        anthropic_compatible_provider="gemma_anthropic",
        anthropic_compatible_base_url="http://gemma.local",
        anthropic_compatible_model="local-model",
        **overrides,
    )


def _azure_default(**overrides) -> Settings:
    """Azure as the default, configured in the OpenAI-compatible slot."""
    return _settings(
        default_provider="azure",
        default_model="gpt-5.4-mini",
        openai_compatible_provider="azure",
        openai_compatible_base_url=AZURE_URL,
        openai_compatible_api_key="azure-key",
        **overrides,
    )


def _router_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "pincer.llm.router" and r.levelno >= logging.WARNING]


# ── voice_turn_target ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", ("gemma_anthropic", "")),
        ("claude-haiku-4-5", ("gemma_anthropic", "claude-haiku-4-5")),
        ("openai:gpt-5-mini", ("openai", "gpt-5-mini")),
        ("azure:gpt-5-mini", ("azure", "gpt-5-mini")),  # unknown prefix is still the provider
        ("ollama:llama3:8b", ("ollama", "llama3:8b")),  # the model keeps its colon
        ("llama3:8b", ("llama3", "8b")),  # voice rule: text before the first colon is the provider
        (" openai : gpt-5-mini ", ("openai", "gpt-5-mini")),
    ],
)
def test_voice_turn_target_parsing(value, expected):
    assert voice_turn_target(_gemma(), value) == expected


def test_voice_turn_target_reads_settings_by_default():
    assert voice_turn_target(_gemma(voice_turn_model="anthropic:claude-haiku")) == ("anthropic", "claude-haiku")


# ── resolve_voice_turn on a custom default provider ──────────────────────────


def test_default_runs_on_router_with_provider_model():
    router = _router(_gemma())
    provider, model = router.resolve_voice_turn()
    assert provider is router  # the router itself: failover kept
    assert model is None
    assert effective_voice_turn(router._settings) == ("gemma_anthropic", "local-model")


def test_stale_openai_override_falls_back_once(caplog):
    router = _router(_gemma(voice_turn_model="openai:gpt-5-mini"))
    caplog.set_level(logging.WARNING, logger="pincer.llm.router")
    with patch.object(router_mod, "_build_leaf_for", wraps=router_mod._build_leaf_for) as build:
        for _ in range(5):
            provider, model = router.resolve_voice_turn()
            assert provider is router
            assert model is None
        assert build.call_count == 1  # never rebuilt per turn
    warnings = _router_warnings(caplog)
    assert len([w for w in warnings if "per-call override" in w]) == 1
    assert len([w for w in warnings if "voice_turn_model" in w]) == 1
    assert "PINCER_OPENAI_API_KEY" in (voice_turn_unusable_reason(router._settings) or "")
    assert effective_voice_turn(router._settings) == ("gemma_anthropic", "local-model")


def test_each_distinct_value_warns_once(caplog):
    router = _router(_gemma(voice_turn_model="openai:gpt-5-mini"))
    caplog.set_level(logging.WARNING, logger="pincer.llm.router")
    router.resolve_voice_turn()
    router._settings.voice_turn_model = "openai:gpt-4o-mini"
    router.resolve_voice_turn()
    router.resolve_voice_turn()
    assert len([w for w in _router_warnings(caplog) if "voice_turn_model" in w]) == 2


def test_anthropic_override_with_key_builds():
    router = _router(_gemma(anthropic_api_key="sk-ant", voice_turn_model="anthropic:claude-haiku-4-5"))
    provider, model = router.resolve_voice_turn()
    assert isinstance(provider, AnthropicCompatibleProvider)
    assert model == "claude-haiku-4-5"


def test_failed_build_without_hint_does_not_block_a_buildable_model():
    """A build that failed for lack of a model hint must not poison the provider
    for a later override that does build (the dashboard accepts that one)."""
    settings = _gemma(openai_api_key="sk-x", voice_turn_model="openai:claude-haiku-4-5")
    router = _router(settings)
    assert router.resolve_voice_turn() == (router, None)
    settings.voice_turn_model = "openai:gpt-5-mini"
    assert voice_turn_unusable_reason(settings) is None
    provider, model = router.resolve_voice_turn()
    assert isinstance(provider, OpenAICompatibleProvider)
    assert model == "gpt-5-mini"


def test_bare_claude_model_on_openai_wire_default_uses_provider_model(caplog):
    router = _router(_azure_default(voice_turn_model="claude-haiku-4-5"))
    caplog.set_level(logging.WARNING, logger="pincer.llm.router")
    for _ in range(3):
        assert router.resolve_voice_turn() == (router, None)
    assert len(_router_warnings(caplog)) == 1


# ── Azure ────────────────────────────────────────────────────────────────────


class TestAzureDefault:
    def test_empty_uses_default_model(self):
        router = _router(_azure_default())
        assert router.resolve_voice_turn() == (router, None)
        assert effective_voice_turn(router._settings) == ("azure", "gpt-5.4-mini")

    def test_azure_prefixed_model(self):
        router = _router(_azure_default(voice_turn_model="azure:gpt-5-mini"))
        assert router.resolve_voice_turn() == (router, "gpt-5-mini")
        assert effective_voice_turn(router._settings) == ("azure", "gpt-5-mini")

    def test_stale_openai_override_falls_back_to_azure(self, caplog):
        router = _router(_azure_default(voice_turn_model="openai:gpt-5-mini"))
        caplog.set_level(logging.WARNING, logger="pincer.llm.router")
        for _ in range(3):
            assert router.resolve_voice_turn() == (router, None)
        assert len([w for w in _router_warnings(caplog) if "voice_turn_model" in w]) == 1
        assert effective_voice_turn(router._settings) == ("azure", "gpt-5.4-mini")


class TestAzureBesideCustomDefault:
    def test_model_hint_lets_azure_build(self):
        """No PINCER_OPENAI_COMPATIBLE_MODEL and a claude-* default model: the
        leaf's fail-fast guard used to reject azure, so voice fell back silently."""
        settings = _gemma(
            openai_compatible_provider="azure",
            openai_compatible_base_url=AZURE_URL,
            openai_compatible_api_key="azure-key",
            voice_turn_model="azure:gpt-5-mini",
        )
        router = _router(settings)
        provider, model = router.resolve_voice_turn()
        assert isinstance(provider, OpenAICompatibleProvider)
        assert provider._provider_name == "azure"
        assert model == "gpt-5-mini"
        assert voice_turn_unusable_reason(settings) is None

    def test_unconfigured_azure_falls_back_once(self, caplog):
        router = _router(_gemma(voice_turn_model="azure:gpt-5-mini"))
        caplog.set_level(logging.WARNING, logger="pincer.llm.router")
        for _ in range(3):
            assert router.resolve_voice_turn() == (router, None)
        assert len([w for w in _router_warnings(caplog) if "voice_turn_model" in w]) == 1
        assert "unknown provider 'azure'" in (voice_turn_unusable_reason(router._settings) or "")

    def test_azure_without_base_url_is_unusable(self):
        settings = _gemma(openai_compatible_provider="azure", voice_turn_model="azure:gpt-5-mini")
        assert "BASE_URL" in (voice_turn_unusable_reason(settings) or "")


# ── Reasoning models on an OpenAI-wire provider ──────────────────────────────


def _kwargs(settings: Settings, model: str) -> dict:
    provider = OpenAICompatibleProvider(settings, "azure")
    return provider._build_kwargs([], None, model, 220, None, None)


def test_reasoning_model_gets_completion_tokens_and_no_temperature():
    kwargs = _kwargs(_azure_default(), "gpt-5-mini")
    assert kwargs["max_completion_tokens"] == 220
    assert "max_tokens" not in kwargs
    assert "temperature" not in kwargs
    assert "reasoning_effort" not in kwargs  # never on a plain (non-voice) request


class _Sent(Exception):
    pass


async def _stream_turn_kwargs(model: str, effort: str | None) -> dict:
    """The request kwargs stream_turn sends (the request itself is cut short)."""
    from unittest.mock import AsyncMock

    provider = OpenAICompatibleProvider(_azure_default(), "azure")
    create = AsyncMock(side_effect=_Sent)
    provider._client.chat.completions.create = create  # type: ignore[method-assign]
    with pytest.raises(_Sent):
        async for _ in provider.stream_turn([], model=model, max_tokens=220, reasoning_effort=effort):
            pass
    return create.call_args.kwargs


async def test_reasoning_effort_sent_only_to_reasoning_models():
    assert (await _stream_turn_kwargs("gpt-5.4-mini", "low"))["reasoning_effort"] == "low"
    assert "reasoning_effort" not in await _stream_turn_kwargs("gpt-5.4-mini", None)
    legacy = await _stream_turn_kwargs("gpt-4o-mini", "low")
    assert "reasoning_effort" not in legacy
    assert legacy["max_tokens"] == 220


def test_voice_reasoning_effort_rejects_minimal():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _azure_default(voice_reasoning_effort="minimal")


# ── One resolver: the dashboard reports what turns really run on ─────────────


@pytest.mark.parametrize(
    "value",
    ["", "openai:gpt-5-mini", "anthropic:claude-haiku-4-5", "azure:gpt-5-mini", "claude-haiku-4-5", "x:y"],
)
@pytest.mark.parametrize("anthropic_key", ["", "sk-ant"])
def test_effective_matches_resolved(value, anthropic_key):
    settings = _gemma(
        anthropic_api_key=anthropic_key,
        openai_compatible_provider="azure",
        openai_compatible_base_url=AZURE_URL,
        voice_turn_model=value,
    )
    router = _router(settings)
    provider, model = router.resolve_voice_turn()
    name = settings.default_provider if provider is router else provider._provider_name
    effective = effective_voice_turn(settings)
    assert effective[0] == name
    assert effective[1] == (model or router_mod.provider_default_model(settings, name))
