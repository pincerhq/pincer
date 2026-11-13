"""What an endpoint takes beyond the basic request (issue #358)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pincer.llm.model_params import ReasoningWire

if TYPE_CHECKING:
    from pincer.config import Settings


@dataclass(frozen=True)
class EndpointCapabilities:
    """The features one provider's endpoint takes. No I/O: read from the settings."""

    server_compaction: bool
    drop_block: bool
    responses_api: bool = False


def endpoint_capabilities(settings: Settings, provider: str) -> EndpointCapabilities:
    """What ``provider``'s endpoint takes: the Anthropic betas when it is in
    ``server_compaction_providers`` (default: ``anthropic`` and the configured compatible name); the
    Responses API when it is in ``responses_api_providers`` (default: ``openai``, ``azure``, ``azure-openai``)."""
    listed = settings.server_compaction_providers
    if listed is None:
        listed = ["anthropic", settings.anthropic_compatible_provider]
    takes_betas = bool(provider) and provider in listed
    return EndpointCapabilities(
        server_compaction=takes_betas,
        drop_block=takes_betas,
        responses_api=bool(provider) and provider in settings.responses_api_providers,
    )


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
