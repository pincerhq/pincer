"""OpenAI GPT provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pincer.llm._openai_common import OpenAICompatibleProvider

if TYPE_CHECKING:
    from pincer.config import Settings


class OpenAIProvider(OpenAICompatibleProvider):
    """OpenAI GPT provider."""

    MODEL_MAP: dict[str, str] = {
        "claude-sonnet-4-5-20250929": "gpt-4o",
        "claude-haiku-4-5-20251001": "gpt-4o-mini",
    }

    def __init__(self, settings: Settings) -> None:
        super().__init__(
            api_key=settings.openai_api_key.get_secret_value(),
            base_url=None,
            settings=settings,
        )
