"""Grok (xAI) provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pincer.llm.openai_compatible_provider import OpenAICompatibleProvider

if TYPE_CHECKING:
    from pincer.config import Settings

GROK_BASE_URL = "https://api.x.ai/v1"


class GrokProvider(OpenAICompatibleProvider):
    """xAI Grok provider via OpenAI-compatible API."""

    MODEL_MAP: dict[str, str] = {
        "claude-sonnet-4-5-20250929": "grok-3",
        "claude-haiku-4-5-20251001": "grok-3-mini",
        "gpt-4o": "grok-3",
        "gpt-4o-mini": "grok-3-mini",
    }

    def __init__(self, settings: Settings) -> None:
        super().__init__(
            api_key=settings.grok_api_key.get_secret_value(),
            base_url=GROK_BASE_URL,
            settings=settings,
        )
