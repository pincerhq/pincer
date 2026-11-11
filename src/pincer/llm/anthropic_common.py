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
    Usage,
)
from anthropic.types import RedactedThinkingBlock as SDKRedactedThinkingBlock
from anthropic.types import ThinkingBlock as SDKThinkingBlock

from pincer.exceptions import LLMError, LLMRateLimitError
from pincer.llm.base import (
    BaseLLMProvider,
    LLMMessage,
    LLMResponse,
    MessageRole,
    StreamTurnEvent,
    ThinkingBlock,
    TokenCounts,
    ToolCall,
    rate_limit_attempts,
    rate_limit_backoff,
    request_client,
)
from pincer.llm.model_params import ReasoningWire, anthropic_accepts_sampling, resolve_and_warn

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pincer.config import Settings
    from pincer.config.llm import ReasoningEffort, ThinkingMode

logger = logging.getLogger(__name__)

# Content block types that carry a response's thinking
_THINKING_TYPES = frozenset({"thinking", "redacted_thinking"})
# Prompt cache breakpoints (owner ruling Q17): tools and system live for an hour, so messages
# minutes apart reuse them; the conversation uses top-level automatic caching (5 minutes)
_ONE_HOUR: dict[str, str] = {"type": "ephemeral", "ttl": "1h"}
_AUTOMATIC: dict[str, str] = {"type": "ephemeral"}


class AnthropicCompatibleProvider(BaseLLMProvider):
    """Provider for any Anthropic-wire endpoint.

    `provider == "anthropic"` uses the well-known API (key + SDK default
    endpoint); any other name uses the configured compatible endpoint
    (base URL + optional key) — local proxies, gateways, etc.
    """

    def __init__(self, settings: Settings, provider: str) -> None:
        self._settings = settings
        self._provider_name = provider

        if provider == "anthropic":
            api_key = settings.anthropic_api_key.get_secret_value()
            if not api_key:
                raise ValueError("provider 'anthropic' needs PINCER_ANTHROPIC_API_KEY")
            base_url: str | None = None
            model = settings.anthropic_model or settings.default_model
        else:
            api_key = settings.anthropic_compatible_api_key.get_secret_value() or "none"
            base_url = settings.anthropic_compatible_base_url
            model = settings.anthropic_compatible_model or settings.default_model

        self._client = AsyncAnthropic(api_key=api_key, base_url=base_url, max_retries=2)
        self._default_model = model
        self._default_max_tokens = settings.max_tokens
        self._default_temperature = settings.temperature
        # Read once: every request of a turn sends the same effort/thinking
        # (changing them mid-conversation invalidates the prompt cache).
        self._reasoning_effort = settings.reasoning_effort
        self._thinking = settings.thinking

    def _cached(self, tools: list[dict[str, Any]] | None, system: str | None) -> dict[str, Any]:
        """The ``tools``, ``system`` and ``cache_control`` kwargs, with the cache breakpoints.

        Only a request with tools is cached: it is an agent loop (chat, voice, headless) whose
        prefix is sent again on every request. One-shot calls (summaries, extractions) send no
        tools, and caching their prompt would only pay the write premium for nothing read back.
        Three breakpoints, in TTL order: the last tool and the end of the system prompt for an
        hour, then the conversation through top-level automatic caching (5 minutes). Messages
        are never marked, so stored history is sent unchanged.
        """
        kwargs: dict[str, Any] = {}
        cache = bool(tools) and getattr(self._settings, "prompt_cache_tools", True)
        if tools:
            kwargs["tools"] = [*tools[:-1], {**tools[-1], "cache_control": dict(_ONE_HOUR)}] if cache else tools
        if system:
            kwargs["system"] = [{"type": "text", "text": system, "cache_control": dict(_ONE_HOUR)}] if cache else system
        if cache:
            kwargs["cache_control"] = dict(_AUTOMATIC)
        return kwargs

    def _build_kwargs(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        max_tokens: int | None,
        temperature: float | None,
        system: str | None,
        reasoning_effort: ReasoningEffort | None = None,
        thinking: ThinkingMode | None = None,
        pinned: bool = False,
    ) -> dict[str, Any]:
        """Request kwargs shared by complete / stream / stream_turn."""
        model = model or self._default_model
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens or self._default_max_tokens,
            "messages": self._convert_messages(messages),
        }
        # A per-call value (voice, summary) replaces the global setting; None keeps it.
        reasoning = resolve_and_warn(
            ReasoningWire.ANTHROPIC,
            model,
            self._reasoning_effort if reasoning_effort is None else reasoning_effort,
            self._thinking if thinking is None else thinking,
            pinned=pinned,
        )
        if not anthropic_accepts_sampling(model):
            logger.debug("Dropping temperature: %s does not accept it", model)
        elif reasoning.drop_temperature:
            logger.debug("Dropping temperature: %s does not accept it with thinking on", model)
        else:
            kwargs["temperature"] = temperature if temperature is not None else self._default_temperature
        kwargs.update(self._cached(tools, system))
        kwargs.update(reasoning.fragment)
        return kwargs

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
        kwargs = self._build_kwargs(
            messages, tools, model, max_tokens, temperature, system, reasoning_effort, thinking, pinned
        )

        try:
            response: Message = await self._call_with_retry(kwargs)
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic API error: {e.status_code} {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"Anthropic connection error: {e}") from e

        result = self._parse_response(response)
        result.provider = self._provider_name
        result.request_model = kwargs["model"]
        return result

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
        """True single-generation streaming (Sprint 5): text deltas arrive as
        the model writes; the final message (tool calls + usage) comes from the
        same stream — tools are never lost, nothing is generated twice.

        A 429 before the first event is retried as `complete()` retries it; once
        text has been yielded nothing is retried, since the caller already has it."""
        kwargs = self._build_kwargs(messages, tools, model, max_tokens, temperature, system, reasoning_effort, thinking)

        attempts = rate_limit_attempts()
        for attempt in range(attempts):
            started = False
            try:
                async with request_client(self._client).messages.stream(**kwargs) as stream:
                    async for text in stream.text_stream:
                        started = True
                        yield StreamTurnEvent(text=text)
                    final: Message = await stream.get_final_message()
                break
            except anthropic.RateLimitError as e:
                retry_after = float(e.response.headers.get("retry-after", "5"))
                if started or attempt == attempts - 1:
                    raise LLMRateLimitError(retry_after=retry_after) from e
                wait = rate_limit_backoff(retry_after, attempt)
                logger.warning("Rate limited, retrying in %.1fs (attempt %d)", wait, attempt + 1)
                await asyncio.sleep(wait)
            except anthropic.APIStatusError as e:
                raise LLMError(f"Anthropic stream error: {e.status_code} {e.message}") from e
            except anthropic.APIConnectionError as e:
                raise LLMError(f"Anthropic connection error: {e}") from e

        result = self._parse_response(final)
        result.provider = self._provider_name
        result.request_model = kwargs["model"]
        yield StreamTurnEvent(response=result)

    async def close(self) -> None:
        await self._client.close()

    # ── Internal ─────────────────────────────────────────

    async def _call_with_retry(self, kwargs: dict[str, Any]) -> Message:
        """Call API with exponential backoff on rate limits.

        The request is streamed although only the final message is used: the
        SDK refuses a non-streaming request whose max_tokens could keep it
        running past its 10-minute timeout (anything above ~21k tokens, less
        for some models), and a streamed one has no such limit.
        """
        attempts = rate_limit_attempts()
        for attempt in range(attempts):
            try:
                async with request_client(self._client).messages.stream(**kwargs) as stream:
                    result: Message = await stream.get_final_message()
                return result
            except anthropic.RateLimitError as e:
                retry_after = float(e.response.headers.get("retry-after", "5"))
                if attempt == attempts - 1:
                    raise LLMRateLimitError(retry_after=retry_after) from e
                wait = rate_limit_backoff(retry_after, attempt)
                logger.warning("Rate limited, retrying in %.1fs (attempt %d)", wait, attempt + 1)
                await asyncio.sleep(wait)
        raise LLMError("Exhausted retries")  # unreachable but satisfies type checker

    def _convert_messages(self, messages: list[LLMMessage]) -> list[dict[str, Any]]:
        """Convert unified messages to Anthropic API format."""
        result: list[dict[str, Any]] = []
        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                # A history summary (the system prompt is the `system` parameter): sent as a
                # user text block of its own, merged with the next user turn below
                result.append({"role": "user", "content": [{"type": "text", "text": msg.content}]})
                continue

            if msg.role == MessageRole.TOOL_RESULT:
                result.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg.tool_call_id,
                                "content": msg.content,
                            }
                        ],
                    }
                )
                continue

            if msg.role == MessageRole.ASSISTANT and msg.tool_calls:
                content_blocks = self._replayed_blocks(msg)
                if content_blocks is None:
                    # Rebuilt: thinking first, then text, then tool_use. Only a message
                    # edited since its response (or built without the recorded order)
                    # gets here; a response's own order is never changed.
                    content_blocks = [self._thinking_to_api(b) for b in msg.thinking_blocks]
                    if msg.content:
                        content_blocks.append({"type": "text", "text": msg.content})
                    for tc in msg.tool_calls:
                        content_blocks.append(
                            {
                                "type": "tool_use",
                                "id": tc.id,
                                "name": tc.name,
                                "input": tc.arguments,
                            }
                        )
                result.append({"role": "assistant", "content": content_blocks})
                continue

            # Thinking with no text after it is not sent: an assistant message
            # holding only thinking is never valid.
            if msg.role == MessageRole.ASSISTANT and msg.thinking_blocks and msg.content:
                replayed = self._replayed_blocks(msg)
                if replayed is None:
                    thinking_first = [self._thinking_to_api(b) for b in msg.thinking_blocks]
                    replayed = [*thinking_first, {"type": "text", "text": msg.content}]
                result.append({"role": "assistant", "content": replayed})
                continue

            # Standard text or text+image, after Pincer's context block when there is one
            if msg.images or msg.context:
                content_parts: list[dict[str, Any]] = []
                if msg.context:
                    content_parts.append({"type": "text", "text": msg.context})
                for img in msg.images:
                    content_parts.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": img.media_type,
                                "data": img.data,
                            },
                        }
                    )
                if msg.content:
                    content_parts.append({"type": "text", "text": msg.content})
                result.append({"role": msg.role.value, "content": content_parts})
            else:
                result.append({"role": msg.role.value, "content": msg.content})

        return self._validate_api_messages(result)

    @staticmethod
    def _replayed_blocks(msg: LLMMessage) -> list[dict[str, Any]] | None:
        """The message's content blocks in the order its response generated them, or None.

        The recorded order is used only while it still describes the message: the same
        tool calls and the same text. A message edited since (a tool call dropped by pair
        repair) is rebuilt instead.
        """
        blocks = msg.content_blocks
        if not (blocks and msg.thinking_blocks):
            return None
        tool_ids = [b["id"] for b in blocks if b["type"] == "tool_use"]
        text = "\n".join(b["text"] for b in blocks if b["type"] == "text")
        if tool_ids != [tc.id for tc in msg.tool_calls] or text != msg.content:
            return None
        return [dict(b) for b in blocks]

    @staticmethod
    def _thinking_to_api(block: ThinkingBlock) -> dict[str, Any]:
        """A thinking block exactly as the response carried it."""
        if block.kind == "redacted_thinking":
            return {"type": "redacted_thinking", "data": block.data}
        return {"type": "thinking", "thinking": block.thinking, "signature": block.signature}

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
                has_tool_use = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks)
                if has_tool_use:
                    surviving = [
                        b
                        for b in blocks
                        if not (isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") not in result_ids)
                    ]
                    if all(_is_thinking(b) for b in surviving):  # thinking alone is never sent
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
                has_tool_result = any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks)
                if has_tool_result:
                    use_ids: set[str] = set()
                    for prev in cleaned:
                        if prev["role"] == "assistant" and isinstance(prev.get("content"), list):
                            for b in prev["content"]:
                                if isinstance(b, dict) and b.get("type") == "tool_use":
                                    use_ids.add(b["id"])
                    surviving = [
                        b
                        for b in blocks
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
        thinking_blocks: list[ThinkingBlock] = []

        ordered: list[dict[str, Any]] = []
        for block in response.content:
            if isinstance(block, TextBlock):
                text_parts.append(block.text)
                ordered.append({"type": "text", "text": block.text})
            elif isinstance(block, SDKThinkingBlock):
                thinking_blocks.append(ThinkingBlock("thinking", thinking=block.thinking, signature=block.signature))
                ordered.append({"type": "thinking", "thinking": block.thinking, "signature": block.signature})
            elif isinstance(block, SDKRedactedThinkingBlock):
                thinking_blocks.append(ThinkingBlock("redacted_thinking", data=block.data))
                ordered.append({"type": "redacted_thinking", "data": block.data})
            elif isinstance(block, ToolUseBlock):
                call = ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=dict(block.input) if isinstance(block.input, dict) else {},
                )
                tool_calls.append(call)
                ordered.append({"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments})

        return LLMResponse(
            content="\n".join(text_parts),
            tool_calls=tool_calls,
            model=response.model,
            stop_reason=response.stop_reason or "",
            **_usage_counts(response.usage),
            thinking_blocks=thinking_blocks,
            # The order matters only to the thinking the next request returns
            content_blocks=ordered if thinking_blocks else [],
        )


def _usage_counts(usage: Usage) -> TokenCounts:
    """The token counts of an Anthropic `usage`: `input_tokens` is already
    uncached input. Cache writes are split by TTL; an endpoint that reports only their
    total (no `cache_creation`) wrote at the default TTL, 5 minutes."""
    written = usage.cache_creation_input_tokens or 0
    by_ttl = usage.cache_creation
    return TokenCounts(
        input_tokens=usage.input_tokens,
        cache_write_5m_tokens=by_ttl.ephemeral_5m_input_tokens if by_ttl is not None else written,
        cache_write_1h_tokens=by_ttl.ephemeral_1h_input_tokens if by_ttl is not None else 0,
        cache_read_tokens=usage.cache_read_input_tokens or 0,
        output_tokens=usage.output_tokens,
    )


def _is_thinking(block: Any) -> bool:
    return isinstance(block, dict) and block.get("type") in _THINKING_TYPES
