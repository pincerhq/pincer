"""Ollama local LLM provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pincer.llm._openai_common import OpenAICompatibleProvider

if TYPE_CHECKING:
    from pincer.config import Settings


class OllamaProvider(OpenAICompatibleProvider):
    """Ollama local LLM provider via OpenAI-compatible API."""

    MODEL_MAP: dict[str, str] = {
        "claude-sonnet-4-5-20250929": "llama3.2",
        "claude-haiku-4-5-20251001": "llama3.2",
        "gpt-4o": "llama3.2",
        "gpt-4o-mini": "llama3.2",
    }

    def __init__(self, settings: Settings) -> None:
        super().__init__(
            api_key="ollama",
            base_url=settings.ollama_base_url,
            settings=settings,
        )
