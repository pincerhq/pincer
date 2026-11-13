"""What an endpoint takes beyond the basic request (issue #358)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pincer.config import Settings


@dataclass(frozen=True)
class EndpointCapabilities:
    """The features one provider's endpoint takes. No I/O: read from the settings."""

    server_compaction: bool
    drop_block: bool


def endpoint_capabilities(settings: Settings, provider: str) -> EndpointCapabilities:
    """What ``provider``'s endpoint takes: the Anthropic betas when it is in
    ``server_compaction_providers`` (default: ``anthropic`` and the configured compatible name)."""
    listed = settings.server_compaction_providers
    if listed is None:
        listed = ["anthropic", settings.anthropic_compatible_provider]
    takes_betas = bool(provider) and provider in listed
    return EndpointCapabilities(server_compaction=takes_betas, drop_block=takes_betas)
