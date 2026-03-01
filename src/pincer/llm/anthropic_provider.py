"""
Anthropic Claude provider implementation.

Supports: tool use, streaming, vision (images), rate limit retry.
Uses the official anthropic Python SDK with AsyncAnthropic.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import anthropic
from anthropic import AsyncAnthropic
from anthropic.types import (
    Message,
    TextBlock,
    ToolUseBlock,
)

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

    from pincer.config import Settings

logger = logging.getLogger(__name__)


class AnthropicProvider(BaseLLMProvider):
    """Claude LLM provider via Anthropic API."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            max_retries=2,
        )
        self._default_model = settings.default_model
        self._default_max_tokens = settings.max_tokens
        self._default_temperature = settings.temperature

    async def complete(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        api_messages = self._convert_messages(messages)
        kwargs: dict[str, Any] = {
            "model": model or self._default_model,
            "max_tokens": max_tokens or self._default_max_tokens,
            "temperature": temperature if temperature is not None else self._default_temperature,
            "messages": api_messages,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools

        try:
            response: Message = await self._call_with_retry(kwargs)
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic API error: {e.status_code} {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"Anthropic connection error: {e}") from e

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
        api_messages = self._convert_messages(messages)
        kwargs: dict[str, Any] = {
            "model": model or self._default_model,
            "max_tokens": max_tokens or self._default_max_tokens,
            "temperature": temperature if temperature is not None else self._default_temperature,
            "messages": api_messages,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools

        try:
            async with self._client.messages.stream(**kwargs) as stream:
                async for text in stream.text_stream:
                    yield text
        except anthropic.RateLimitError as e:
            retry_after = float(e.response.headers.get("retry-after", "5"))
            raise LLMRateLimitError(retry_after=retry_after) from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic stream error: {e.status_code}") from e

    async def close(self) -> None:
        await self._client.close()

    # ── Internal ─────────────────────────────────────────

    async def _call_with_retry(
        self,
        kwargs: dict[str, Any],
        max_retries: int = 3,
    ) -> Message:
        """Call API with exponential backoff on rate limits."""
        for attempt in range(max_retries):
            try:
                return await self._client.messages.create(**kwargs)
            except anthropic.RateLimitError as e:
                retry_after = float(e.response.headers.get("retry-after", "5"))
                if attempt == max_retries - 1:
                    raise LLMRateLimitError(retry_after=retry_after) from e
                wait = min(retry_after * (2**attempt), 60)
                logger.warning(
                    "Rate limited, retrying in %.1fs (attempt %d)", wait, attempt + 1
                )
                await asyncio.sleep(wait)
        raise LLMError("Exhausted retries")  # unreachable but satisfies type checker

    def _convert_messages(self, messages: list[LLMMessage]) -> list[dict[str, Any]]:
        """Convert unified messages to Anthropic API format."""
        result: list[dict[str, Any]] = []
        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                continue  # system handled separately

            if msg.role == MessageRole.TOOL_RESULT:
                result.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg.tool_call_id,
                            "content": msg.content,
                        }
                    ],
                })
                continue

            if msg.role == MessageRole.ASSISTANT and msg.tool_calls:
                content_blocks: list[dict[str, Any]] = []
                if msg.content:
                    content_blocks.append({"type": "text", "text": msg.content})
                for tc in msg.tool_calls:
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.arguments,
                    })
                result.append({"role": "assistant", "content": content_blocks})
                continue

            # Standard text or text+image
            if msg.images:
                content_parts: list[dict[str, Any]] = []
                for img in msg.images:
                    content_parts.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": img.media_type,
                            "data": img.data,
                        },
                    })
                if msg.content:
                    content_parts.append({"type": "text", "text": msg.content})
                result.append({"role": msg.role.value, "content": content_parts})
            else:
                result.append({"role": msg.role.value, "content": msg.content})

        return self._validate_api_messages(result)

    @staticmethod
    def _validate_api_messages(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Enforce Anthropic constraints on the converted message list.

        Rules:
        1. Every assistant message containing tool_use blocks must be
           immediately followed by user message(s) with matching tool_result
           blocks for ALL tool_use IDs.
        2. Consecutive same-role messages are merged (Anthropic rejects them).
        3. The first non-system message must be role=user.
        """
        if not messages:
            return messages

        # Pass 1: collect tool_use IDs that have matching tool_result
        result_ids: set[str] = set()
        for msg in messages:
            if msg["role"] == "user" and isinstance(msg.get("content"), list):
                for block in msg["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        result_ids.add(block["tool_use_id"])

        # Pass 2: strip assistant tool_use messages whose IDs lack results
        cleaned: list[dict[str, Any]] = []
        for msg in messages:
            if msg["role"] == "assistant" and isinstance(msg.get("content"), list):
                blocks = msg["content"]
                has_tool_use = any(
                    isinstance(b, dict) and b.get("type") == "tool_use"
                    for b in blocks
                )
                if has_tool_use:
                    surviving = [
                        b for b in blocks
                        if not (
                            isinstance(b, dict)
                            and b.get("type") == "tool_use"
                            and b.get("id") not in result_ids
                        )
                    ]
                    if not surviving:
                        logger.debug("Dropping assistant message: all tool_use blocks orphaned")
                        continue
                    if len(surviving) != len(blocks):
                        logger.debug(
                            "Stripped %d orphaned tool_use blocks from assistant message",
                            len(blocks) - len(surviving),
                        )
                    msg = {**msg, "content": surviving}

            # Strip orphaned tool_result (no matching tool_use)
            if msg["role"] == "user" and isinstance(msg.get("content"), list):
                blocks = msg["content"]
                has_tool_result = any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in blocks
                )
                if has_tool_result:
                    use_ids: set[str] = set()
                    for prev in cleaned:
                        if prev["role"] == "assistant" and isinstance(prev.get("content"), list):
                            for b in prev["content"]:
                                if isinstance(b, dict) and b.get("type") == "tool_use":
                                    use_ids.add(b["id"])
                    surviving = [
                        b for b in blocks
                        if not (
                            isinstance(b, dict)
                            and b.get("type") == "tool_result"
                            and b.get("tool_use_id") not in use_ids
                        )
                    ]
                    if not surviving:
                        continue
                    msg = {**msg, "content": surviving}

            cleaned.append(msg)

        # Pass 3: merge consecutive same-role messages
        merged: list[dict[str, Any]] = []
        for msg in cleaned:
            if merged and merged[-1]["role"] == msg["role"]:
                prev = merged[-1]
                prev_content = prev["content"]
                cur_content = msg["content"]
                if isinstance(prev_content, str) and isinstance(cur_content, str):
                    merged[-1] = {**prev, "content": prev_content + "\n" + cur_content}
                elif isinstance(prev_content, list) and isinstance(cur_content, list):
                    merged[-1] = {**prev, "content": prev_content + cur_content}
                elif isinstance(prev_content, str) and isinstance(cur_content, list):
                    merged[-1] = {**prev, "content": [{"type": "text", "text": prev_content}] + cur_content}
                elif isinstance(prev_content, list) and isinstance(cur_content, str):
                    merged[-1] = {**prev, "content": prev_content + [{"type": "text", "text": cur_content}]}
            else:
                merged.append(msg)

        # Pass 4: ensure first message is role=user
        if merged and merged[0]["role"] != "user":
            merged.insert(0, {"role": "user", "content": "(continuing conversation)"})

        return merged

    def _parse_response(self, response: Message) -> LLMResponse:
        """Parse Anthropic Message into unified LLMResponse."""
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for block in response.content:
            if isinstance(block, TextBlock):
                text_parts.append(block.text)
            elif isinstance(block, ToolUseBlock):
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=dict(block.input) if isinstance(block.input, dict) else {},
                    )
                )

        return LLMResponse(
            content="\n".join(text_parts),
            tool_calls=tool_calls,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            stop_reason=response.stop_reason or "",
        )
