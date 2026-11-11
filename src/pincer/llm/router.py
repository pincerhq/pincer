"""
Single entry point for LLM provider construction + failover.

`LLMRouter` is itself a `BaseLLMProvider`: `complete()`/`stream_turn()` try the primary
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
from pincer.core.log_once import LogOncePerKey
from pincer.exceptions import LLMError, LLMRateLimitError
from pincer.llm.anthropic_common import AnthropicCompatibleProvider
from pincer.llm.base import BaseLLMProvider, LLMMessage, LLMResponse, StreamTurnEvent, rate_limit_retries
from pincer.llm.openai_common import OpenAICompatibleProvider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from pincer.config import Settings
    from pincer.config.llm import ReasoningEffort, ThinkingMode

    # (provider name, model hint) → the provider, or why it can't be built
    BuildFn = Callable[[str, str], BaseLLMProvider | str]

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


def known_provider_names(settings: Settings) -> set[str]:
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
    if sep and prefix.strip() in known_provider_names(settings):
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


def is_free_provider(settings: Settings, name: str) -> bool:
    """A provider is free when it is a compatible endpoint with no API key (e.g. a local Ollama)."""
    if name in WELL_KNOWN_PROVIDERS:
        return False
    if name and name == settings.openai_compatible_provider:
        return settings.openai_compatible_api_key.get_secret_value() == ""
    if name and name == settings.anthropic_compatible_provider:
        return settings.anthropic_compatible_api_key.get_secret_value() == ""
    return False


def has_credentials(settings: Settings, name: str) -> bool:
    """True when `name` can be built from the configured credentials: a key for a
    well-known provider, a base URL for a compatible endpoint (its key is optional)."""
    if name == "anthropic":
        return settings.anthropic_api_key.get_secret_value() != ""
    if name == "openai":
        return settings.openai_api_key.get_secret_value() != ""
    if name and name == settings.openai_compatible_provider:
        return settings.openai_compatible_base_url != ""
    if name and name == settings.anthropic_compatible_provider:
        return settings.anthropic_compatible_base_url != ""
    return False


def wire_safe_model(settings: Settings, name: str, model: str) -> str:
    """``model``, or ``""`` (the provider's own default) when it is an Anthropic
    id bound for an OpenAI-wire provider, which would reject it. The one rule
    the summarizer, the voice turn and the dashboard choices share."""
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


def try_build_provider(settings: Settings, name: str, model_hint: str = "") -> BaseLLMProvider | str:
    """The leaf for ``name`` built from the configured credentials, or why it
    can't be. Nothing is sent over the network."""
    try:
        return _build_leaf_for(settings, name, model_hint)
    except Exception as e:
        return str(e)


@dataclass(frozen=True, eq=False)
class VoiceTurnPlan:
    """What a voice turn runs on — the one answer the agent, the startup check
    and the dashboard all read."""

    requested: str  # the voice_turn_model value, stripped
    provider: str  # provider the turn runs on
    model: str  # model to request; "" = that provider's own default
    effective_model: str  # the model that really answers (``model`` or the provider's default)
    leaf: BaseLLMProvider | None  # the built provider when it isn't the default one
    unusable: str | None  # why the requested provider can't run (the turn fell back to the default)
    wire_adjusted: bool  # a claude-* id was dropped for an OpenAI-wire provider


def plan_voice_turn(settings: Settings, value: str | None = None, *, build: BuildFn | None = None) -> VoiceTurnPlan:
    """Resolve a voice turn model (see ``voice_turn_target``) to what really runs.

    The default provider is always usable (the pool is validated at startup);
    any other provider must build. ``build`` defaults to ``try_build_provider``;
    the router passes its memoised one, which builds the same way, so the
    dashboard and the call get the same answer.
    """
    raw = str(getattr(settings, "voice_turn_model", "") if value is None else value).strip()
    name, model = voice_turn_target(settings, raw)
    safe = wire_safe_model(settings, name, model)
    leaf: BaseLLMProvider | None = None
    unusable: str | None = None
    if name != settings.default_provider:
        built = (build or (lambda n, h: try_build_provider(settings, n, h)))(name, safe)
        if isinstance(built, str):
            unusable = built
        else:
            leaf = built
    if unusable is not None:
        # A foreign model id would 404 on the default provider: use its own model.
        name, safe = settings.default_provider, ""
    return VoiceTurnPlan(
        requested=raw,
        provider=name,
        model=safe,
        effective_model=safe or provider_default_model(settings, name),
        leaf=leaf,
        unusable=unusable,
        wire_adjusted=unusable is None and safe != model,
    )


class LLMRouter(BaseLLMProvider):
    """Single construction + failover entry point. Drop-in `BaseLLMProvider`."""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._pool: dict[str, BaseLLMProvider] | None = None
        # Providers outside the configured pool, built on demand per
        # (provider, model_hint): the leaf, or why it can't be built. Never
        # rebuilt (or re-logged) per call — credentials don't change while the
        # process runs, and the hint decides whether an OpenAI-wire leaf with
        # no model of its own builds, so it is part of the key.
        self._on_demand: dict[tuple[str, str], BaseLLMProvider | str] = {}
        # One line per voice_turn_model value, not per turn
        self._voice_log = LogOncePerKey(logger)

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

    def _tries(self) -> list[BaseLLMProvider]:
        """The providers one request is sent to, in order, until one answers: the primary, the
        failovers in random order, then the primary again (Q24). Only that last try retries a
        429 (Q23: fail over first). Without failovers: the primary alone, with its retry."""
        primary, failovers = self._primary_and_failovers()
        if not failovers:
            return [primary]
        return [primary, *random.sample(failovers, len(failovers)), primary]

    # ── Accessors ────────────────────────────────────────

    def get_llm(self) -> BaseLLMProvider:
        self._build_pool()  # validate eagerly at construction time
        return self

    def get_summarizer(self) -> tuple[BaseLLMProvider, str | None]:
        """Provider and model for compaction summaries (see `summary_target`).

        A provider outside the pool is built on demand. ``None`` as the model
        means "the provider's own default model".
        """
        self._build_pool()
        name, model = summary_target(self._settings)
        if not wire_safe_model(self._settings, name, model):
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
        """Provider by name — from the configured pool, or built on demand when
        its credentials exist (memoised per ``model_hint``, see ``_on_demand``).
        Used by the voice turn-model override (e.g. "openai:gpt-5-mini" while the
        default provider stays anthropic) and the summarizer. Returns None when
        unavailable; a failure is logged once, never rebuilt per call.

        ``model_hint``: the model the caller will request (see ``_build_leaf_for``).
        """
        pool = self._build_pool()
        if name in pool:
            return pool[name]
        key = (name, model_hint)
        if key not in self._on_demand:
            built = try_build_provider(self._settings, name, model_hint)
            if isinstance(built, str):
                logger.warning("Provider %r unavailable for per-call override: %s", name, built)
            self._on_demand[key] = built
        found = self._on_demand[key]
        return None if isinstance(found, str) else found

    def _provider_or_reason(self, name: str, model_hint: str) -> BaseLLMProvider | str:
        """``plan_voice_turn``'s build step through ``get_provider``."""
        provider = self.get_provider(name, model_hint=model_hint)
        if provider is not None:
            return provider
        reason = self._on_demand.get((name, model_hint))
        return reason if isinstance(reason, str) else f"provider {name!r} is unavailable"

    def resolve_voice_turn(self, value: str | None = None) -> tuple[BaseLLMProvider, VoiceTurnPlan]:
        """Provider to stream one voice turn on, and the plan behind it.

        ``value`` is the caller's ``voice_turn_model`` (the agent passes its own
        live settings, which the dashboard updates); ``None`` reads the router's.
        The default provider is served by the router itself, so a voice turn
        keeps failover. An override whose provider can't be built falls back to
        the default provider with its own model. Each fallback is logged once
        per distinct value.
        """
        plan = plan_voice_turn(self._settings, value, build=self._provider_or_reason)
        if plan.wire_adjusted:
            self._voice_log.log(
                f"{plan.requested}#wire",
                "voice_turn_model %r is an Anthropic model id but provider %r is OpenAI-wire — "
                "using that provider's default model instead",
                plan.requested,
                plan.provider,
            )
        if plan.unusable is not None:
            self._voice_log.log(
                plan.requested,
                "voice_turn_model %r is unavailable (%s) — using the default provider %r and its model",
                plan.requested,
                plan.unusable,
                plan.provider,
            )
        if plan.leaf is None:
            self._build_pool()
            return self, plan
        return plan.leaf, plan

    def is_free(self, provider: str) -> bool:
        """A provider is free when it has no API key (e.g. a local Ollama endpoint)."""
        return is_free_provider(self._settings, provider)

    # ── BaseLLMProvider ──────────────────────────────────

    async def complete(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        thinking: ThinkingMode | None = None,
        pinned: bool = False,
    ) -> LLMResponse:
        """Providers in the order of ``_tries`` until one answers, as in ``stream_turn``:
        every try but the last raises a 429 at once; the last one (the primary again)
        retries it, and its error is the one raised. An explicit ``model`` goes to the
        primary only: failovers use their own model (see ``stream_turn``)."""
        *first_tries, last_try = self._tries()
        for index, provider in enumerate(first_tries):
            try:
                with rate_limit_retries(False):
                    return await provider.complete(
                        messages,
                        tools=tools,
                        model=model if index == 0 else None,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        system=system,
                        reasoning_effort=reasoning_effort,
                        thinking=thinking,
                        pinned=pinned,
                    )
            except (LLMError, LLMRateLimitError) as err:
                logger.warning("complete provider %d failed: %s — trying next", index, err)
        return await last_try.complete(
            messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            reasoning_effort=reasoning_effort,
            thinking=thinking,
            pinned=pinned,
        )

    async def stream_turn(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        thinking: ThinkingMode | None = None,
    ) -> AsyncIterator[StreamTurnEvent]:
        """Failover discipline: providers are tried in the order of ``_tries``
        until one produces its first event; a mid-stream failure propagates.
        Every try but the last raises a 429 at once, so the next one takes over
        without a wait; the last one (the primary again) retries it, and its
        error is the one raised.

        An explicit ``model`` is the primary's id (e.g. a voice turn's
        ``azure:gpt-5-mini`` on an azure default): a failover leaf of another
        vendor would 404 on it, so failovers get ``None`` — their own model."""
        *first_tries, last_try = self._tries()
        for index, provider in enumerate(first_tries):
            stream = provider.stream_turn(
                messages,
                tools=tools,
                model=model if index == 0 else None,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system,
                reasoning_effort=reasoning_effort,
                thinking=thinking,
            )
            try:
                # Only the first event: the provider's retries all happen before it, and the
                # setting must not be in force while our consumer handles an event.
                with rate_limit_retries(False):
                    first = await anext(stream, None)
            except (LLMError, LLMRateLimitError) as err:
                logger.warning("stream_turn provider %d failed before first event: %s — trying next", index, err)
                continue
            if first is None:
                return
            yield first
            async for event in stream:  # a mid-stream failure propagates — no mid-stream failover
                yield event
            return
        async for event in last_try.stream_turn(
            messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            reasoning_effort=reasoning_effort,
            thinking=thinking,
        ):
            yield event

    async def close(self) -> None:
        if self._pool:
            for leaf in self._pool.values():
                await leaf.close()
        for built in self._on_demand.values():
            if not isinstance(built, str):
                await built.close()
