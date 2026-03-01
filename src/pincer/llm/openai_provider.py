"""
OpenAI provider implementation.

Supports: function calling (tool use), streaming, vision.
Uses the official openai Python SDK with AsyncOpenAI.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

import openai
from openai import AsyncOpenAI

from pincer.exceptions import LLMError, LLMRateLimitError
from pincer.llm.base import (
    BaseLLMProvider,
    LLMMessage,
    LLMResponse,
    MessageRole,
    ToolCall,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from openai.types.chat import ChatCompletion

    from pincer.config import Settings

logger = logging.getLogger(__name__)


def _convert_tools_to_openai(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Anthropic-style tool defs to OpenAI function-calling format."""
    oai_tools: list[dict[str, Any]] = []
    for tool in tools:
        oai_tools.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {}),
            },
        })
    return oai_tools


class OpenAIProvider(BaseLLMProvider):
    """OpenAI GPT provider."""

    MODEL_MAP: dict[str, str] = {
        "claude-sonnet-4-5-20250929": "gpt-4o",
        "claude-haiku-4-5-20251001": "gpt-4o-mini",
    }

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = AsyncOpenAI(
            api_key=settings.openai_api_key.get_secret_value(),
            max_retries=2,
        )
        self._default_model = self._resolve_model(settings.default_model)
        self._default_max_tokens = settings.max_tokens
        self._default_temperature = settings.temperature

    def _resolve_model(self, model: str) -> str:
        return self.MODEL_MAP.get(model, model)

    async def complete(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        api_messages = self._convert_messages(messages, system)
        kwargs: dict[str, Any] = {
            "model": self._resolve_model(model) if model else self._default_model,
            "max_tokens": max_tokens or self._default_max_tokens,
            "temperature": temperature if temperature is not None else self._default_temperature,
            "messages": api_messages,
        }
        if tools:
            kwargs["tools"] = _convert_tools_to_openai(tools)

        try:
            response = await self._call_with_retry(kwargs)
        except openai.APIStatusError as e:
            raise LLMError(f"OpenAI API error: {e.status_code} {e.message}") from e
        except openai.APIConnectionError as e:
            raise LLMError(f"OpenAI connection error: {e}") from e

        return self._parse_response(response)

    async def stream(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
    ) -> AsyncIterator[str]:
        api_messages = self._convert_messages(messages, system)
        kwargs: dict[str, Any] = {
            "model": self._resolve_model(model) if model else self._default_model,
            "max_tokens": max_tokens or self._default_max_tokens,
            "temperature": temperature if temperature is not None else self._default_temperature,
            "messages": api_messages,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = _convert_tools_to_openai(tools)

        try:
            stream = await self._client.chat.completions.create(**kwargs)
            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    yield delta.content
        except openai.RateLimitError as e:
            raise LLMRateLimitError(retry_after=5.0) from e
        except openai.APIStatusError as e:
            raise LLMError(f"OpenAI stream error: {e.status_code}") from e

    async def close(self) -> None:
        await self._client.close()

    # ── Internal ─────────────────────────────────────────

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
                logger.warning("Rate limited, retrying in %.1fs", wait)
                await asyncio.sleep(wait)
        raise LLMError("Exhausted retries")

    def _convert_messages(
        self,
        messages: list[LLMMessage],
        system: str | None = None,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        if system:
            result.append({"role": "system", "content": system})

        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                result.append({"role": "system", "content": msg.content})
            elif msg.role == MessageRole.TOOL_RESULT:
                result.append({
                    "role": "tool",
                    "tool_call_id": msg.tool_call_id or "",
                    "content": msg.content,
                })
            elif msg.role == MessageRole.ASSISTANT and msg.tool_calls:
                tool_calls_api = []
                for tc in msg.tool_calls:
                    tool_calls_api.append({
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    })
                result.append({
                    "role": "assistant",
                    "content": msg.content or None,
                    "tool_calls": tool_calls_api,
                })
            elif msg.images:
                content_parts: list[dict[str, Any]] = []
                for img in msg.images:
                    content_parts.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{img.media_type};base64,{img.data}",
                        },
                    })
                if msg.content:
                    content_parts.append({"type": "text", "text": msg.content})
                result.append({"role": msg.role.value, "content": content_parts})
            else:
                result.append({"role": msg.role.value, "content": msg.content})

        return result

    def _parse_response(self, response: ChatCompletion) -> LLMResponse:
        choice = response.choices[0]
        message = choice.message
        tool_calls: list[ToolCall] = []

        if message.tool_calls:
            for tc in message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append(
                    ToolCall(id=tc.id, name=tc.function.name, arguments=args)
                )

        return LLMResponse(
            content=message.content or "",
            tool_calls=tool_calls,
            model=response.model,
            input_tokens=response.usage.prompt_tokens if response.usage else 0,
            output_tokens=response.usage.completion_tokens if response.usage else 0,
            stop_reason=choice.finish_reason or "",
        )
