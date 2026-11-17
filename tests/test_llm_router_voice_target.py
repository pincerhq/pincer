"""Voice turn target resolution: which provider/model a voice turn really runs on (#305)."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from pincer.config import Settings
from pincer.llm import router as router_mod
from pincer.llm.anthropic_common import AnthropicCompatibleProvider
from pincer.llm.openai_common import OpenAICompatibleProvider
from pincer.llm.router import LLMRouter, plan_voice_turn, voice_turn_target

AZURE_URL = "https://res.openai.azure.com/openai/v1/"


def _settings(**overrides) -> Settings:
    base = {"telegram_bot_token": "123456:TEST", "anthropic_api_key": "", "openai_api_key": ""}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _resolve(router: LLMRouter, value: str | None = None):
    """(provider, model-or-None) the way the agent reads ``resolve_voice_turn``."""
    provider, plan = router.resolve_voice_turn(value)
    return provider, plan.model or None


def effective_voice_turn(settings: Settings, value: str | None = None) -> tuple[str, str]:
    plan = plan_voice_turn(settings, value)
    return plan.provider, plan.effective_model


def voice_turn_unusable_reason(settings: Settings, value: str | None = None) -> str | None:
    return plan_voice_turn(settings, value).unusable


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
    provider, model = _resolve(router)
    assert provider is router  # the router itself: failover kept
    assert model is None
    assert effective_voice_turn(router._settings) == ("gemma_anthropic", "local-model")


def test_stale_openai_override_falls_back_once(caplog):
    router = _router(_gemma(voice_turn_model="openai:gpt-5-mini"))
    caplog.set_level(logging.WARNING, logger="pincer.llm.router")
    with patch.object(router_mod, "_build_leaf_for", wraps=router_mod._build_leaf_for) as build:
        for _ in range(5):
            provider, model = _resolve(router)
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
    _resolve(router)
    router._settings.voice_turn_model = "openai:gpt-4o-mini"
    _resolve(router)
    _resolve(router)
    assert len([w for w in _router_warnings(caplog) if "voice_turn_model" in w]) == 2


def test_anthropic_override_with_key_builds():
    router = _router(_gemma(anthropic_api_key="sk-ant", voice_turn_model="anthropic:claude-haiku-4-5"))
    provider, model = _resolve(router)
    assert isinstance(provider, AnthropicCompatibleProvider)
    assert model == "claude-haiku-4-5"


def test_failed_build_without_hint_does_not_block_a_buildable_model():
    """A build that failed for lack of a model hint must not poison the provider
    for a later override that does build (the dashboard accepts that one)."""
    settings = _gemma(openai_api_key="sk-x", voice_turn_model="openai:claude-haiku-4-5")
    router = _router(settings)
    assert _resolve(router) == (router, None)
    settings.voice_turn_model = "openai:gpt-5-mini"
    assert voice_turn_unusable_reason(settings) is None
    provider, model = _resolve(router)
    assert isinstance(provider, OpenAICompatibleProvider)
    assert model == "gpt-5-mini"


def test_bare_claude_model_on_openai_wire_default_uses_provider_model(caplog):
    router = _router(_azure_default(voice_turn_model="claude-haiku-4-5"))
    caplog.set_level(logging.WARNING, logger="pincer.llm.router")
    for _ in range(3):
        assert _resolve(router) == (router, None)
    assert len(_router_warnings(caplog)) == 1


# ── Azure ────────────────────────────────────────────────────────────────────


class TestAzureDefault:
    def test_empty_uses_default_model(self):
        router = _router(_azure_default())
        assert _resolve(router) == (router, None)
        assert effective_voice_turn(router._settings) == ("azure", "gpt-5.4-mini")

    def test_azure_prefixed_model(self):
        router = _router(_azure_default(voice_turn_model="azure:gpt-5-mini"))
        assert _resolve(router) == (router, "gpt-5-mini")
        assert effective_voice_turn(router._settings) == ("azure", "gpt-5-mini")

    def test_stale_openai_override_falls_back_to_azure(self, caplog):
        router = _router(_azure_default(voice_turn_model="openai:gpt-5-mini"))
        caplog.set_level(logging.WARNING, logger="pincer.llm.router")
        for _ in range(3):
            assert _resolve(router) == (router, None)
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
        provider, model = _resolve(router)
        assert isinstance(provider, OpenAICompatibleProvider)
        assert provider._provider_name == "azure"
        assert model == "gpt-5-mini"
        assert voice_turn_unusable_reason(settings) is None

    def test_unconfigured_azure_falls_back_once(self, caplog):
        router = _router(_gemma(voice_turn_model="azure:gpt-5-mini"))
        caplog.set_level(logging.WARNING, logger="pincer.llm.router")
        for _ in range(3):
            assert _resolve(router) == (router, None)
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
    kwargs = _kwargs(_azure_default(reasoning_effort="", thinking=""), "gpt-5-mini")
    assert kwargs["max_completion_tokens"] == 220
    assert "max_tokens" not in kwargs
    assert "temperature" not in kwargs
    assert "reasoning_effort" not in kwargs  # the global setting is empty here


class _Sent(Exception):
    pass


async def _stream_turn_kwargs(model: str, effort: str | None) -> dict:
    """The request kwargs stream_turn sends (the request itself is cut short)."""
    from unittest.mock import AsyncMock

    # Global effort/thinking pinned empty: the developer's .env must not leak in. On Chat Completions:
    # these tests are about its request kwargs
    chat = _azure_default(reasoning_effort="", thinking="", responses_api_providers=[])
    provider = OpenAICompatibleProvider(chat, "azure")
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
    provider, model = _resolve(router)
    name = settings.default_provider if provider is router else provider._provider_name
    effective = effective_voice_turn(settings)
    assert effective[0] == name
    assert effective[1] == (model or router_mod.provider_default_model(settings, name))


# ── Review round: failover model, memo agreement, the agent's own value ──────


async def test_explicit_model_goes_to_the_primary_only():
    """azure:gpt-5-mini on an azure default with an anthropic failover: the
    failover must get its own model, not the Azure deployment name."""
    from pincer.exceptions import LLMError
    from pincer.llm.base import BaseLLMProvider, StreamTurnEvent

    class Leaf(BaseLLMProvider):
        def __init__(self, fail: bool) -> None:
            self.fail, self.models = fail, []

        async def complete(self, messages, tools=None, model=None, **kw):  # type: ignore[override]
            self.models.append(model)
            if self.fail:
                raise LLMError("down")
            return router_mod.LLMResponse(content="ok", model=model or "", provider="")

        async def stream_turn(self, messages, tools=None, model=None, **kw):  # type: ignore[override]
            self.models.append(model)
            if self.fail:
                raise LLMError("down")
            yield StreamTurnEvent(text="ok")

        async def close(self) -> None:
            pass

    settings = _azure_default(fallback_providers=["anthropic"], anthropic_api_key="sk-ant")
    router = _router(settings)
    azure, anthropic = Leaf(fail=True), Leaf(fail=False)
    router._pool = {"azure": azure, "anthropic": anthropic}

    [e async for e in router.stream_turn([], model="gpt-5-mini")]
    await router.complete([], model="gpt-5-mini")
    assert azure.models == ["gpt-5-mini"] * 2
    assert anthropic.models == [None] * 2


def test_router_and_dashboard_agree_after_another_hint_built_the_provider():
    """A leaf built earlier for one hint must not make another hint look usable
    to the call while the dashboard (and the startup check) says it isn't."""
    settings = _settings(
        default_provider="anthropic",
        default_model="claude-sonnet-4-5",
        anthropic_api_key="sk-ant",
        openai_api_key="sk-x",
    )
    router = _router(settings)
    assert isinstance(_resolve(router, "openai:gpt-5-mini")[0], OpenAICompatibleProvider)
    provider, plan = router.resolve_voice_turn("openai:claude-haiku-4-5")
    static = plan_voice_turn(settings, "openai:claude-haiku-4-5")
    assert (plan.provider, plan.effective_model, plan.unusable is None) == (
        static.provider,
        static.effective_model,
        static.unusable is None,
    )
    assert provider is router  # both: the default provider and its model


def test_resolve_uses_the_value_passed_in():
    """The agent passes its own live settings' value — the one PUT /config
    updates — even when the router's settings object is a different one."""
    router = _router(_gemma(anthropic_api_key="sk-ant", voice_turn_model=""))
    provider, plan = router.resolve_voice_turn("anthropic:claude-haiku-4-5")
    assert isinstance(provider, AnthropicCompatibleProvider)
    assert (plan.provider, plan.model) == ("anthropic", "claude-haiku-4-5")
    assert _resolve(router) == (router, None)  # None still reads the router's own


def test_plan_carries_the_built_leaf():
    settings = _gemma(anthropic_api_key="sk-ant")
    plan = plan_voice_turn(settings, "anthropic:claude-haiku-4-5")
    assert isinstance(plan.leaf, AnthropicCompatibleProvider)
    assert plan_voice_turn(settings, "").leaf is None
    assert plan_voice_turn(settings, "openai:gpt-5-mini").leaf is None  # fell back


@pytest.mark.parametrize(
    ("name", "model", "expected"),
    [
        ("openai", "claude-haiku-4-5", ""),
        ("azure", "claude-haiku-4-5", ""),
        ("openai", "gpt-5-mini", "gpt-5-mini"),
        ("anthropic", "claude-haiku-4-5", "claude-haiku-4-5"),
    ],
)
def test_wire_safe_model(name, model, expected):
    settings = _azure_default()
    assert router_mod.wire_safe_model(settings, name, model) == expected


# ── reasoning_effort allow-list ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("model", "accepted"),
    [
        ("gpt-5", True),
        ("gpt-5-mini", True),
        ("gpt-5.4-mini", True),
        ("gpt-5-2025-08-07", True),
        ("gpt-5.1-codex-mini", True),
        ("o1", True),
        ("o3-mini", True),
        ("o4-mini", True),
        ("openai/gpt-5-nano", True),
        ("ft:gpt-5-mini:org::abc", True),
        ("gpt-5-chat-latest", False),
        ("o1-mini", False),
        ("o1-preview", False),
        ("gpt-4o-mini", False),
        ("voice-fast", False),  # an Azure deployment name that doesn't look like a model id
        # Responses API only (v1/chat/completions not supported), per their model pages (#345)
        ("o1-pro", False),
        ("o3-pro", False),
        ("gpt-5-pro", False),
        ("gpt-5.2-pro", False),
    ],
)
def test_openai_accepts_reasoning_effort(model, accepted):
    from pincer.llm.model_params import openai_accepts_reasoning_effort

    assert openai_accepts_reasoning_effort(model) is accepted


async def test_effort_not_sent_to_a_model_that_rejects_it(caplog):
    """Off the allow-list: nothing sent, one WARNING per model and value (#345 raised it from INFO)."""
    caplog.set_level(logging.INFO, logger="pincer.llm.openai_common")
    for _ in range(2):
        assert "reasoning_effort" not in await _stream_turn_kwargs("gpt-5-chat-latest", "low")
    skipped = [r for r in caplog.records if "Not sending reasoning_effort=low to gpt-5-chat-latest" in r.getMessage()]
    assert [r.levelno for r in skipped] == [logging.WARNING]  # once per model and value


@pytest.mark.parametrize("effort", ["xhigh", "max"])
async def test_levels_above_high_step_to_xhigh_on_gpt_5_2_plus(effort):
    """gpt-5.2+ takes xhigh; max steps down to it (#345)."""
    assert (await _stream_turn_kwargs("gpt-5.4-mini", effort))["reasoning_effort"] == "xhigh"


# ── LogOncePerKey ────────────────────────────────────────────────────────────


def test_log_once_per_key_is_bounded(caplog):
    from pincer.core.log_once import LogOncePerKey

    log = logging.getLogger("test.log_once_per_key")
    once = LogOncePerKey(log, max_keys=2)
    caplog.set_level(logging.DEBUG, logger="test.log_once_per_key")
    for key in ["a", "a", "b", "c", "a"]:
        once.log(key, "warn %s", key)
    levels = [(r.getMessage(), r.levelno) for r in caplog.records]
    assert levels == [
        ("warn a", logging.WARNING),
        ("warn a", logging.DEBUG),
        ("warn b", logging.WARNING),
        ("warn c", logging.WARNING),  # evicts "a", the oldest
        ("warn a", logging.WARNING),  # forgotten, so it logs once more
    ]
    assert len(once._seen) == 2
