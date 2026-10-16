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
from typing import TYPE_CHECKING, Any

from pincer.config import get_settings
from pincer.exceptions import LLMError, LLMRateLimitError
from pincer.llm.anthropic_common import AnthropicCompatibleProvider
from pincer.llm.base import BaseLLMProvider, LLMMessage, LLMResponse, StreamTurnEvent
from pincer.llm.openai_common import OpenAICompatibleProvider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

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


class LLMRouter(BaseLLMProvider):
    """Single construction + failover entry point. Drop-in `BaseLLMProvider`."""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._pool: dict[str, BaseLLMProvider] | None = None

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
        its credentials exist. Used by the voice turn-model override
        (Sprint 5: e.g. voice_turn_model="openai:gpt-5-mini" while the default
        provider stays anthropic). Returns None when unavailable.

        ``model_hint``: the model the caller will request. Lets an on-demand
        OpenAI-wire leaf construct even when its default model would resolve
        to a claude-* id (its fail-fast guard), since every voice call passes
        the model explicitly anyway.
        """
        pool = self._build_pool()
        if name in pool:
            return pool[name]
        settings = self._settings
        if model_hint and name == "openai" and not settings.openai_model:
            settings = settings.model_copy(update={"openai_model": model_hint})
        try:
            provider = _build_leaf(settings, name)
        except Exception as e:
            logger.warning("Provider %r unavailable for per-call override: %s", name, e)
            return None
        pool[name] = provider  # memoise alongside the configured pool
        return provider

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
    ) -> AsyncIterator[StreamTurnEvent]:
        """Same failover discipline as stream(): providers are tried in order
        until one produces its first event; a mid-stream failure propagates."""
        primary, failovers = self._primary_and_failovers()
        ordered = [primary, *random.sample(failovers, len(failovers))]
        primary_err: Exception | None = None
        for index, provider in enumerate(ordered):
            started = False
            try:
                async for event in provider.stream_turn(
                    messages, tools=tools, model=model, max_tokens=max_tokens, temperature=temperature, system=system
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
