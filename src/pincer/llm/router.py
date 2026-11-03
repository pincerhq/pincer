"""
Single entry point for LLM provider construction + failover.

`LLMRouter` is itself a `BaseLLMProvider`: `complete()`/`stream()` try the primary
provider and, on failure, fall over to a randomly-ordered failover provider. All
provider construction lives here — `cli.py` and `api/_deps.py` only call
`LLMRouter().get_llm()` / `.get_summarizer()`.

Provider names are free-form. `openai`/`anthropic` are well-known (key only); any
other name must match a configured `*_compatible_provider` (base URL + optional key).
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pincer.config import get_settings
from pincer.exceptions import LLMError, LLMRateLimitError
from pincer.llm.anthropic_common import AnthropicCompatibleProvider
from pincer.llm.base import BaseLLMProvider, LLMMessage, LLMResponse, StreamTurnEvent
from pincer.llm.openai_common import OpenAICompatibleProvider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from pincer.config import Settings

logger = logging.getLogger(__name__)

WELL_KNOWN_PROVIDERS: frozenset[str] = frozenset({"openai", "anthropic"})
MAX_FAILOVERS = 3


def _build_leaf(settings: Settings, name: str) -> BaseLLMProvider:
    """Resolve a provider name to a constructed provider instance."""
    if name == "openai":
        return OpenAICompatibleProvider(settings, "openai")
    if name == "anthropic":
        return AnthropicCompatibleProvider(settings, "anthropic")
    if name and name == settings.openai_compatible_provider:
        if not settings.openai_compatible_base_url:
            raise ValueError(f"provider {name!r} needs PINCER_OPENAI_COMPATIBLE_BASE_URL")
        return OpenAICompatibleProvider(settings, name)
    if name and name == settings.anthropic_compatible_provider:
        if not settings.anthropic_compatible_base_url:
            raise ValueError(f"provider {name!r} needs PINCER_ANTHROPIC_COMPATIBLE_BASE_URL")
        return AnthropicCompatibleProvider(settings, name)
    raise ValueError(
        f"unknown provider {name!r}; expected 'openai', 'anthropic', "
        f"or a configured PINCER_OPENAI_COMPATIBLE_PROVIDER / PINCER_ANTHROPIC_COMPATIBLE_PROVIDER"
    )


def _known_provider_names(settings: Settings) -> set[str]:
    names = {*WELL_KNOWN_PROVIDERS, settings.openai_compatible_provider, settings.anthropic_compatible_provider}
    names.discard("")
    return names


def is_openai_wire(settings: Settings, name: str) -> bool:
    """True when `name` resolves to an OpenAI-wire provider."""
    return name == "openai" or (bool(name) and name == settings.openai_compatible_provider)


def summary_target(settings: Settings) -> tuple[str, str]:
    """(provider name, model) the summarizer asks for.

    ``summary_model`` may be ``provider:model`` (like ``voice_turn_model``); the
    prefix only counts when it is a known provider name, so ids with colons
    (Ollama's ``llama3:8b``) stay whole. A prefix overrides ``summary_provider``;
    with neither, the default provider is used.
    """
    provider = settings.summary_provider or ""
    model = settings.summary_model
    prefix, sep, rest = model.partition(":")
    if sep and prefix.strip() in _known_provider_names(settings):
        provider, model = prefix.strip(), rest.strip()
    return provider or settings.default_provider, model


def voice_turn_target(settings: Settings, value: str | None = None) -> tuple[str, str]:
    """(provider name, model) a voice turn asks for.

    ``value`` defaults to ``settings.voice_turn_model``:

    - ``""`` → the default provider and its own default model (``""``);
    - ``"<model>"`` → that model on the default provider;
    - ``"<provider>:<model>"`` → that provider and model. The text before the
      first colon is always the provider, so ``ollama:llama3:8b`` keeps the
      colon in its model. Unlike ``summary_target``, an unknown prefix is not
      folded into the model id: a misspelt or unconfigured provider must fall
      back to the default model, never send ``azure:gpt-5-mini`` as one id.
    """
    raw = str(getattr(settings, "voice_turn_model", "") if value is None else value).strip()
    prefix, sep, rest = raw.partition(":")
    if sep:
        return prefix.strip() or settings.default_provider, rest.strip()
    return settings.default_provider, raw


def provider_default_model(settings: Settings, name: str) -> str:
    """The model a provider uses when a call passes none (mirrors the leaf constructors)."""
    if name == "openai":
        return settings.openai_model or settings.default_model
    if name == "anthropic":
        return settings.anthropic_model or settings.default_model
    if name and name == settings.openai_compatible_provider:
        return settings.openai_compatible_model or settings.default_model
    if name and name == settings.anthropic_compatible_provider:
        return settings.anthropic_compatible_model or settings.default_model
    return settings.default_model


def _wire_safe_model(settings: Settings, name: str, model: str) -> str:
    """``""`` (the provider's default) when ``model`` is an Anthropic id bound for an OpenAI-wire provider."""
    if model.startswith("claude") and is_openai_wire(settings, name):
        return ""
    return model


def _build_leaf_for(settings: Settings, name: str, model_hint: str = "") -> BaseLLMProvider:
    """``_build_leaf`` for a per-call model.

    ``model_hint`` is the model the caller will request. An OpenAI-wire leaf
    with no model of its own falls back to ``default_model``, which trips its
    claude-* fail-fast guard on an Anthropic default; the hint stands in as its
    default instead, since the caller passes the model explicitly anyway.
    """
    if model_hint and not model_hint.startswith("claude"):
        if name == "openai" and not settings.openai_model:
            settings = settings.model_copy(update={"openai_model": model_hint})
        elif name and name == settings.openai_compatible_provider and not settings.openai_compatible_model:
            settings = settings.model_copy(update={"openai_compatible_model": model_hint})
    return _build_leaf(settings, name)


@dataclass(frozen=True)
class VoiceTurnPlan:
    """What a voice turn runs on — the one answer the agent, the startup check
    and the dashboard all read, so they can't disagree."""

    requested: str  # the voice_turn_model value, stripped
    provider: str  # provider the turn runs on
    model: str  # model to request; "" = that provider's own default
    unusable: str | None  # why the requested provider can't run (the turn fell back to the default)
    wire_adjusted: bool  # a claude-* id was dropped for an OpenAI-wire provider


def _static_build_check(settings: Settings) -> Callable[[str, str], str | None]:
    """Build check without a router: construct the leaf, keep only the error."""

    def check(name: str, model_hint: str) -> str | None:
        try:
            _build_leaf_for(settings, name, model_hint)
        except Exception as e:
            return str(e)
        return None

    return check


def plan_voice_turn(
    settings: Settings,
    value: str | None = None,
    *,
    build_check: Callable[[str, str], str | None] | None = None,
) -> VoiceTurnPlan:
    """Resolve a voice turn model (see ``voice_turn_target``) to what really runs.

    The default provider is always usable (the pool is validated at startup).
    Any other provider must build from the configured credentials — nothing is
    sent over the network. ``build_check(name, model_hint)`` returns why it
    can't (``None`` when it can); the router passes its memoised one.
    """
    raw = str(getattr(settings, "voice_turn_model", "") if value is None else value).strip()
    name, model = voice_turn_target(settings, raw)
    safe = _wire_safe_model(settings, name, model)
    unusable: str | None = None
    if name != settings.default_provider:
        unusable = (build_check or _static_build_check(settings))(name, safe)
    if unusable is not None:
        # A foreign model id would 404 on the default provider: use its own model.
        return VoiceTurnPlan(raw, settings.default_provider, "", unusable, wire_adjusted=False)
    return VoiceTurnPlan(raw, name, safe, None, wire_adjusted=safe != model)


def voice_turn_unusable_reason(settings: Settings, value: str | None = None) -> str | None:
    """Why a voice turn model can't run here, or ``None`` when it can."""
    return plan_voice_turn(settings, value).unusable


def effective_voice_turn(settings: Settings, value: str | None = None) -> tuple[str, str]:
    """(provider, model) a voice turn really runs on — the default when the override can't."""
    plan = plan_voice_turn(settings, value)
    return plan.provider, plan.model or provider_default_model(settings, plan.provider)


class LLMRouter(BaseLLMProvider):
    """Single construction + failover entry point. Drop-in `BaseLLMProvider`."""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._pool: dict[str, BaseLLMProvider] | None = None
        # (provider, model_hint) pairs that failed to build on demand: never
        # rebuilt (or re-logged) per call. Credentials don't change while the
        # process runs, but the hint does decide whether an OpenAI-wire leaf
        # with no model of its own builds, so it is part of the key.
        self._unavailable: set[tuple[str, str]] = set()
        # voice_turn_model values already warned about (one line per value, not per turn)
        self._voice_warned: set[str] = set()

    # ── Construction ─────────────────────────────────────

    def _build_pool(self) -> dict[str, BaseLLMProvider]:
        """Build every configured provider once (memoised), with validation."""
        if self._pool is not None:
            return self._pool

        s = self._settings
        if len(s.fallback_providers) > MAX_FAILOVERS:
            raise ValueError(f"At most {MAX_FAILOVERS} failover providers allowed, got {len(s.fallback_providers)}")
        for cname in (s.openai_compatible_provider, s.anthropic_compatible_provider):
            if cname in WELL_KNOWN_PROVIDERS:
                raise ValueError(f"compatible provider name {cname!r} collides with a well-known provider")

        pool: dict[str, BaseLLMProvider] = {}
        for name in [s.default_provider, *s.fallback_providers]:
            if name not in pool:
                pool[name] = _build_leaf(s, name)
        self._pool = pool
        return pool

    def _primary_and_failovers(self) -> tuple[BaseLLMProvider, list[BaseLLMProvider]]:
        pool = self._build_pool()
        primary = pool[self._settings.default_provider]
        failovers = [pool[n] for n in self._settings.fallback_providers]
        return primary, failovers

    # ── Accessors ────────────────────────────────────────

    def get_llm(self) -> BaseLLMProvider:
        self._build_pool()  # validate eagerly at construction time
        return self

    def get_summarizer(self) -> tuple[BaseLLMProvider, str | None]:
        """Provider and model for the Summarizer (see `summary_target`).

        A provider outside the pool is built on demand. ``None`` as the model
        means "the provider's own default model".
        """
        self._build_pool()
        name, model = summary_target(self._settings)
        if model.startswith("claude") and is_openai_wire(self._settings, name):
            # Typically the claude-haiku summary_model default on an OpenAI setup:
            # an OpenAI-wire endpoint would reject it, so use the provider's own model.
            logger.warning(
                "summary_model %r is an Anthropic model id but the summary provider %r is OpenAI-wire — "
                "using that provider's default model instead",
                model,
                name,
            )
            model = ""
        provider = self.get_provider(name, model_hint=model)
        if provider is None:
            raise ValueError(f"summary provider {name!r} could not be built (missing credentials or base URL?)")
        return provider, model or None

    def get_provider(self, name: str, model_hint: str = "") -> BaseLLMProvider | None:
        """Provider by name — from the active pool, or built on demand when
        its credentials exist. Used by the voice turn-model override (e.g.
        voice_turn_model="openai:gpt-5-mini" while the default provider stays
        anthropic). Returns None when unavailable; a failure is logged and
        remembered once, never rebuilt per call.

        ``model_hint``: the model the caller will request (see ``_build_leaf_for``).
        """
        pool = self._build_pool()
        if name in pool:
            return pool[name]
        if (name, model_hint) in self._unavailable:
            return None
        try:
            provider = _build_leaf_for(self._settings, name, model_hint)
        except Exception as e:
            logger.warning("Provider %r unavailable for per-call override: %s", name, e)
            self._unavailable.add((name, model_hint))
            return None
        pool[name] = provider  # memoise alongside the configured pool
        return provider

    def resolve_voice_turn(self) -> tuple[BaseLLMProvider, str | None]:
        """Provider and model for one voice turn (see ``plan_voice_turn``).

        The default provider is served by the router itself, so a voice turn
        keeps failover. An override whose provider can't be built falls back
        to the default provider with its own model (``None``). Each fallback
        is logged once per distinct ``voice_turn_model`` value.
        """
        s = self._settings
        plan = plan_voice_turn(s, build_check=self._build_check)
        if plan.wire_adjusted:
            self._warn_voice_once(
                f"{plan.requested}#wire",
                "voice_turn_model %r is an Anthropic model id but provider %r is OpenAI-wire — "
                "using that provider's default model instead",
                plan.requested,
                plan.provider,
            )
        if plan.unusable is not None:
            self._warn_voice_once(
                plan.requested,
                "voice_turn_model %r is unavailable (%s) — using the default provider %r and its model",
                plan.requested,
                plan.unusable,
                s.default_provider,
            )
        if plan.provider == s.default_provider:
            self._build_pool()
            return self, plan.model or None
        provider = self.get_provider(plan.provider, model_hint=plan.model)
        if provider is None:  # can't happen: plan_voice_turn just built it through the same memo
            return self, None
        return provider, plan.model or None

    def _build_check(self, name: str, model_hint: str) -> str | None:
        """``plan_voice_turn``'s build check through the memoised pool."""
        if self.get_provider(name, model_hint=model_hint) is not None:
            return None
        return f"provider {name!r} could not be built (see the earlier warning)"

    def _warn_voice_once(self, key: str, msg: str, *args: object) -> None:
        if key in self._voice_warned:
            return
        self._voice_warned.add(key)
        logger.warning(msg, *args)

    def is_free(self, provider: str) -> bool:
        """A provider is free when it has no API key (e.g. a local Ollama endpoint)."""
        s = self._settings
        if provider in WELL_KNOWN_PROVIDERS:
            return False
        if provider and provider == s.openai_compatible_provider:
            return s.openai_compatible_api_key.get_secret_value() == ""
        if provider and provider == s.anthropic_compatible_provider:
            return s.anthropic_compatible_api_key.get_secret_value() == ""
        return False

    # ── BaseLLMProvider ──────────────────────────────────

    async def complete(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        primary, failovers = self._primary_and_failovers()
        try:
            return await primary.complete(
                messages, tools=tools, model=model, max_tokens=max_tokens, temperature=temperature, system=system
            )
        except (LLMError, LLMRateLimitError) as primary_err:
            for provider in random.sample(failovers, len(failovers)):
                try:
                    return await provider.complete(
                        messages,
                        tools=tools,
                        model=model,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        system=system,
                    )
                except (LLMError, LLMRateLimitError) as err:
                    logger.warning("Failover provider failed: %s — trying next", err)
                    continue
            raise primary_err

    async def stream(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
    ) -> AsyncIterator[str]:
        primary, failovers = self._primary_and_failovers()
        ordered = [primary, *random.sample(failovers, len(failovers))]
        primary_err: Exception | None = None
        for index, provider in enumerate(ordered):
            started = False
            try:
                async for token in provider.stream(
                    messages, tools=tools, model=model, max_tokens=max_tokens, temperature=temperature, system=system
                ):
                    started = True
                    yield token
                return
            except (LLMError, LLMRateLimitError) as err:
                if index == 0:
                    primary_err = err
                if started:
                    raise  # mid-stream failure propagates — no mid-stream failover
                logger.warning("Stream provider %d failed before first token: %s — trying next", index, err)
                continue
        if primary_err is not None:
            raise primary_err

    async def stream_turn(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
        reasoning_effort: str | None = None,
    ) -> AsyncIterator[StreamTurnEvent]:
        """Same failover discipline as stream(): providers are tried in order
        until one produces its first event; a mid-stream failure propagates."""
        primary, failovers = self._primary_and_failovers()
        ordered = [primary, *random.sample(failovers, len(failovers))]
        primary_err: Exception | None = None
        # Passed on only when set, so a leaf without the knob keeps its signature.
        extra: dict[str, Any] = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}
        for index, provider in enumerate(ordered):
            started = False
            try:
                async for event in provider.stream_turn(
                    messages,
                    tools=tools,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=system,
                    **extra,
                ):
                    started = True
                    yield event
                return
            except (LLMError, LLMRateLimitError) as err:
                if index == 0:
                    primary_err = err
                if started:
                    raise  # mid-stream failure propagates — no mid-stream failover
                logger.warning("stream_turn provider %d failed before first event: %s — trying next", index, err)
                continue
        if primary_err is not None:
            raise primary_err

    async def close(self) -> None:
        if self._pool:
            for leaf in self._pool.values():
                await leaf.close()
