"""
Base class for OpenAI-compatible LLM providers.

Subclasses supply only MODEL_MAP and __init__ credentials.
Shared implementation covers complete(), stream(), _call_with_retry().
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import openai
from openai import AsyncOpenAI

from pincer.exceptions import LLMError, LLMRateLimitError
from pincer.llm._openai_common import (
    convert_messages_to_openai,
    convert_tools_to_openai,
    parse_openai_response,
)
from pincer.llm.base import BaseLLMProvider, LLMMessage, LLMResponse

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from openai.types.chat import ChatCompletion

    from pincer.config import Settings

logger = logging.getLogger(__name__)


class OpenAICompatibleProvider(BaseLLMProvider):
    """Base for any OpenAI-compatible provider. Subclasses set MODEL_MAP and __init__."""

    MODEL_MAP: dict[str, str] = {}

    def __init__(self, api_key: str, base_url: str | None, settings: Settings) -> None:
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            max_retries=2,
        )
        self._default_model = self._resolve_model(settings.default_model)
        self._default_max_tokens = settings.max_tokens
        self._default_temperature = settings.temperature

    def _resolve_model(self, model: str) -> str:
        return self.MODEL_MAP.get(model, model)

    @property
    def _provider_name(self) -> str:
        return type(self).__name__.replace("Provider", "")

    async def complete(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        api_messages = convert_messages_to_openai(messages, system)
        kwargs: dict[str, Any] = {
            "model": self._resolve_model(model) if model else self._default_model,
            "max_tokens": max_tokens or self._default_max_tokens,
            "temperature": temperature if temperature is not None else self._default_temperature,
            "messages": api_messages,
        }
        if tools:
            kwargs["tools"] = convert_tools_to_openai(tools)

        try:
            response = await self._call_with_retry(kwargs)
        except openai.APIStatusError as e:
            raise LLMError(f"{self._provider_name} API error: {e.status_code} {e.message}") from e
        except openai.APIConnectionError as e:
            raise LLMError(f"{self._provider_name} connection error: {e}") from e

        return parse_openai_response(response)

    async def stream(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
    ) -> AsyncIterator[str]:
        api_messages = convert_messages_to_openai(messages, system)
        kwargs: dict[str, Any] = {
            "model": self._resolve_model(model) if model else self._default_model,
            "max_tokens": max_tokens or self._default_max_tokens,
            "temperature": temperature if temperature is not None else self._default_temperature,
            "messages": api_messages,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = convert_tools_to_openai(tools)

        try:
            stream = await self._client.chat.completions.create(**kwargs)
            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    yield delta.content
        except openai.RateLimitError as e:
            raise LLMRateLimitError(retry_after=5.0) from e
        except openai.APIStatusError as e:
            raise LLMError(f"{self._provider_name} stream error: {e.status_code}") from e

    async def close(self) -> None:
        await self._client.close()

    async def _call_with_retry(
        self,
        kwargs: dict[str, Any],
        max_retries: int = 3,
    ) -> ChatCompletion:
        for attempt in range(max_retries):
            try:
                return await self._client.chat.completions.create(**kwargs)
            except openai.RateLimitError as e:
                if attempt == max_retries - 1:
                    raise LLMRateLimitError(retry_after=5.0) from e
                wait = min(5.0 * (2**attempt), 60)
                logger.warning("%s rate limited, retrying in %.1fs", self._provider_name, wait)
                await asyncio.sleep(wait)
        raise LLMError("Exhausted retries")
