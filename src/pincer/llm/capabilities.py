"""What an endpoint takes beyond the basic request (issue #358)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import anthropic
import openai

from pincer.llm.model_params import ReasoningWire

if TYPE_CHECKING:
    from pincer.config import Settings

logger = logging.getLogger(__name__)

# Anthropic beta names an endpoint can reject
_COMPACTION_BETA = "compact-2026-09-04"
_BINDING_BETA = "thinking-binding-controls-2026-08-01"
# The two message shapes that prove an Anthropic-wire endpoint lacks a beta (deliberately not the bare
# words: the prefix-check 400 names `block_binding` and `compaction_*` codes name `compaction`)
_UNKNOWN_BETA = re.compile(r"Unexpected value\(s\) (.+?) for the `anthropic-beta` header")
_EXTRA_FIELD = re.compile(r"(?:^|[\s.`'\"])(block_binding|compaction): Extra inputs are not permitted")
# A 404 with one of these body codes says the model or deployment is missing, not the endpoint
_MISSING_MODEL_CODES = frozenset({"model_not_found", "DeploymentNotFound"})
_MISSING_ENDPOINT_STATUSES = frozenset({404, 405, 501})


class Feature(StrEnum):
    """What an endpoint can prove it lacks (issue #358, D6)."""

    RESPONSES_API = "responses-api"  # /responses -> Chat Completions
    RESPONSES_COMPACT = "responses-compact"  # /responses/compact -> the summary model
    SERVER_COMPACTION = "server-compaction"  # Anthropic's compaction beta -> the summary model
    DROP_BLOCK = "drop-block"  # Anthropic's thinking-binding beta -> none


# The feature each beta name stands for
_BETA_FEATURES = {_COMPACTION_BETA: Feature.SERVER_COMPACTION, _BINDING_BETA: Feature.DROP_BLOCK}
# What the process logs once when an endpoint loses a feature, and what doctor lists
_SWITCHED_OFF_LOG = {
    Feature.RESPONSES_API: "Endpoint %s answered %d on /responses: it has no Responses API, so it uses Chat "
    "Completions from now on (until restart); reasoning is no longer kept across turns on it",
    Feature.RESPONSES_COMPACT: "Endpoint %s answered %d on /responses/compact: it has no server compaction, so its "
    "conversations are compacted by the summary model from now on (until restart)",
    Feature.SERVER_COMPACTION: "Endpoint %s rejected the compaction beta (HTTP %d): server compaction is off for it, "
    "so its conversations are compacted by the summary model and stored compaction blocks are sent as text, "
    "from now on (until restart)",
    Feature.DROP_BLOCK: "Endpoint %s rejected the thinking-binding beta (HTTP %d): drop_block is off for it from "
    "now on (until restart)",
}
_SWITCHED_OFF_LABEL = {
    Feature.RESPONSES_API: "Responses API (HTTP %d) -> Chat Completions",
    Feature.RESPONSES_COMPACT: "/responses/compact (HTTP %d) -> summary model",
    Feature.SERVER_COMPACTION: "server compaction (HTTP %d) -> summary model",
    Feature.DROP_BLOCK: "drop_block (HTTP %d) -> off",
}


@dataclass(frozen=True)
class SwitchedOff:
    endpoint: str
    feature: Feature
    status: int

    @property
    def label(self) -> str:
        return f"{self.endpoint}: {_SWITCHED_OFF_LABEL[self.feature] % self.status}"


class FeatureSwitches:
    """The features endpoints proved they lack, remembered for the life of the process (in memory)."""

    def __init__(self) -> None:
        self._off: dict[tuple[str, Feature], int] = {}

    def is_off(self, endpoint: str, feature: Feature) -> bool:
        return (endpoint, feature) in self._off

    def switch_off(self, endpoint: str, feature: Feature, status: int) -> bool:
        """Remember that `endpoint` lacks `feature`; True the first time, which logs the WARNING once."""
        if (endpoint, feature) in self._off:
            return False
        self._off[(endpoint, feature)] = status
        logger.warning(_SWITCHED_OFF_LOG[feature], endpoint, status)
        return True

    def listed(self) -> list[SwitchedOff]:
        return [SwitchedOff(endpoint, feature, status) for (endpoint, feature), status in sorted(self._off.items())]

    def reset(self) -> None:
        self._off.clear()


SWITCHES = FeatureSwitches()


@dataclass(frozen=True)
class EndpointCapabilities:
    """The features one provider's endpoint takes: what the settings list, minus what it proved it lacks."""

    server_compaction: bool
    drop_block: bool
    responses_api: bool = False
    responses_compact: bool = False


def endpoint_capabilities(settings: Settings, provider: str) -> EndpointCapabilities:
    """What ``provider``'s endpoint takes: the Anthropic betas when it is in
    ``server_compaction_providers`` (default: ``anthropic`` and the configured compatible name); the
    Responses API when it is in ``responses_api_providers`` (default: ``openai``, ``azure``, ``azure-openai``)."""
    listed = settings.server_compaction_providers
    if listed is None:
        listed = ["anthropic", settings.anthropic_compatible_provider]
    takes_betas = bool(provider) and provider in listed
    responses_api = (
        bool(provider)
        and provider in settings.responses_api_providers
        and not SWITCHES.is_off(provider, Feature.RESPONSES_API)
    )
    return EndpointCapabilities(
        server_compaction=takes_betas and not SWITCHES.is_off(provider, Feature.SERVER_COMPACTION),
        drop_block=takes_betas and not SWITCHES.is_off(provider, Feature.DROP_BLOCK),
        responses_api=responses_api,
        responses_compact=responses_api and not SWITCHES.is_off(provider, Feature.RESPONSES_COMPACT),
    )


def unsupported_features(error: BaseException) -> frozenset[Feature]:
    """The features an API error proves the endpoint lacks; empty for every other error (a 429, a 5xx, any
    other 400 or 404). Only these shapes count:

    - OpenAI wire: 404, 405 or 501 on `/responses`; 404 on `/responses/compact`; in neither case a 404 whose
      body code names a missing model or deployment (`model_not_found`, `DeploymentNotFound`);
    - Anthropic wire, a 400 whose message is Anthropic's unknown-beta text naming a beta, or
      `<field>: Extra inputs are not permitted` for `block_binding` or `compaction`.
    """
    if isinstance(error, openai.APIStatusError):
        if _error_code(error) in _MISSING_MODEL_CODES:
            return frozenset()  # the model or deployment is missing, not the endpoint's feature
        path = error.response.request.url.path.rstrip("/")
        if path.endswith("/responses/compact"):
            return frozenset({Feature.RESPONSES_COMPACT}) if error.status_code == 404 else frozenset()
        if path.endswith("/responses") and error.status_code in _MISSING_ENDPOINT_STATUSES:
            return frozenset({Feature.RESPONSES_API})
        return frozenset()
    if isinstance(error, anthropic.APIStatusError) and error.status_code == 400:
        message = _anthropic_message(error)
        features: set[Feature] = set()
        if (beta := _UNKNOWN_BETA.search(message)) is not None:
            named = re.findall(r"`([^`]+)`", beta.group(1))
            features |= {_BETA_FEATURES[name] for name in named if name in _BETA_FEATURES}
        if (field := _EXTRA_FIELD.search(message)) is not None:
            features.add(Feature.DROP_BLOCK if field.group(1) == "block_binding" else Feature.SERVER_COMPACTION)
        return frozenset(features)
    return frozenset()


def _error_code(error: openai.APIStatusError) -> str | None:
    body: Any = error.body
    code = body.get("code") if isinstance(body, dict) else None
    return code if isinstance(code, str) else None


def _anthropic_message(error: anthropic.APIStatusError) -> str:
    body: Any = error.body
    inner = body.get("error") if isinstance(body, dict) else None
    message = inner.get("message") if isinstance(inner, dict) else None
    return message if isinstance(message, str) else ""


def reasoning_wire(settings: Settings, provider: str) -> ReasoningWire | None:
    """The request format ``provider``'s endpoint speaks, which is what reasoning settings are resolved for:
    the Responses API or Chat Completions for an OpenAI-wire endpoint (see ``EndpointCapabilities``), the
    Messages API for an Anthropic-wire one, None for a name that is not configured. The one place that
    decides it: the providers, the startup check, ``pincer doctor`` and the smoke script all ask here."""
    from pincer.llm.router import is_openai_wire  # the router imports the providers, which import this module

    if is_openai_wire(settings, provider):
        return (
            ReasoningWire.OPENAI_RESPONSES
            if endpoint_capabilities(settings, provider).responses_api
            else ReasoningWire.OPENAI
        )
    if provider == "anthropic" or (provider and provider == settings.anthropic_compatible_provider):
        return ReasoningWire.ANTHROPIC
    return None


def learn_from_error(settings: Settings, provider: str, error: BaseException, before: EndpointCapabilities) -> bool:
    """Switch off the features `error` proves `provider`'s endpoint lacks (the WARNING is logged on the first
    switch only), and say whether the endpoint's capabilities now differ from `before`, those the failed
    request was built with. True means the request is retried once in the fallback mode: it holds when
    another request switched the feature off first (two can fail at once), and cannot hold twice."""
    status = error.status_code if isinstance(error, openai.APIStatusError | anthropic.APIStatusError) else 0
    for feature in unsupported_features(error):
        SWITCHES.switch_off(provider, feature, status)
    return endpoint_capabilities(settings, provider) != before
