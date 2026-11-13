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

from pincer.exceptions import LLMError, LLMRateLimitError
from pincer.llm.base import (
    BaseLLMProvider,
    LLMMessage,
    LLMResponse,
    MessageRole,
    RequestClients,
    StreamTurnEvent,
    ThinkingDrop,
    TokenCounts,
    ToolCall,
    rate_limit_attempts,
    rate_limit_backoff,
    transient_status,
)
from pincer.llm.capabilities import endpoint_capabilities
from pincer.llm.model_params import ReasoningWire, anthropic_accepts_sampling, resolve_and_warn, thinking_is_bound
from pincer.llm.replay import replayable_raw, wire_tag

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from anthropic.types import Message, Usage
    from anthropic.types.beta import BetaMessage, BetaUsage
    from anthropic.types.beta.beta_compaction_iteration_usage import BetaCompactionIterationUsage

    from pincer.config import Settings
    from pincer.config.llm import ReasoningEffort, ThinkingMode

logger = logging.getLogger(__name__)

# Content block types that carry a response's thinking
_THINKING_TYPES = frozenset({"thinking", "redacted_thinking"})
# Prompt cache breakpoints (owner ruling Q17): tools and system live for an hour, so messages
# minutes apart reuse them; the conversation uses top-level automatic caching (5 minutes)
_ONE_HOUR: dict[str, str] = {"type": "ephemeral", "ttl": "1h"}
_AUTOMATIC: dict[str, str] = {"type": "ephemeral"}
# Anthropic's thinking-block binding controls (issue #358): `thinking.block_binding` is accepted with this beta
_THINKING_BINDING_BETA = "thinking-binding-controls-2026-08-01"
# On-demand compaction (issue #358): the request that asks for a summary carries it
_COMPACTION_BETA = "compact-2026-09-04"
_COMPACTION_PARAMETER = {"compaction": {"type": "summarize"}}


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
        self._request_clients = RequestClients[AsyncAnthropic]()
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
            "messages": self._convert_messages(messages, model),
        }
        if any(_has_compaction(message) for message in kwargs["messages"]):
            kwargs["betas"] = [_COMPACTION_BETA]  # every request that carries the signed block needs it
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
        if endpoint_capabilities(self._settings, self._provider_name).drop_block and thinking_is_bound(
            model, reasoning
        ):
            # Earlier thinking goes back to the model, and a block that no longer fits its history is
            # dropped by the API instead of failing the request (the strict flag makes it fail, for tests)
            behavior = "error" if self._settings.strict_thinking_binding else "drop_block"
            kwargs["thinking"] = {
                **kwargs.get("thinking", {"type": "adaptive"}),
                "block_binding": {"prefix_mismatch_behavior": behavior},
            }
            kwargs["betas"] = [*kwargs.get("betas", []), _THINKING_BINDING_BETA]
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
            message = f"Anthropic API error: {e.status_code} {e.message}"
            raise LLMError(message, retryable=transient_status(e.status_code)) from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"Anthropic connection error: {e}") from e

        result = self._parse_response(response)
        result.provider = self._provider_name
        result.request_model = kwargs["model"]
        result.wire = wire_tag(self._provider_name, kwargs["model"])
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
                async with self._messages_api(kwargs).stream(**kwargs) as stream:
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
                message = f"Anthropic stream error: {e.status_code} {e.message}"
                raise LLMError(message, retryable=transient_status(e.status_code)) from e
            except anthropic.APIConnectionError as e:
                raise LLMError(f"Anthropic connection error: {e}") from e

        result = self._parse_response(final)
        result.provider = self._provider_name
        result.request_model = kwargs["model"]
        result.wire = wire_tag(self._provider_name, kwargs["model"])
        yield StreamTurnEvent(response=result)

    async def compact(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        system: str | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        thinking: ThinkingMode | None = None,
    ) -> LLMResponse:
        """Ask the server to summarize `messages` (on-demand compaction).

        The request is the one a normal turn sends for the same arguments (model, thinking,
        system, tools, messages) plus the `compaction` parameter and its beta, so the prompt is
        a cache read and the thinking in the turns kept after the block stays valid. The call
        runs with the provider's default `max_tokens` (it caps the summary and any thinking
        before it). A summary comes back as `stop_reason == "compaction"` with the block in `raw`.
        """
        kwargs = self._build_kwargs(messages, tools, model, None, None, system, reasoning_effort, thinking)
        kwargs["betas"] = [_COMPACTION_BETA, *(b for b in kwargs.get("betas", []) if b != _COMPACTION_BETA)]
        kwargs["extra_body"] = dict(_COMPACTION_PARAMETER)
        try:
            response: Message = await self._call_with_retry(kwargs)
        except anthropic.APIStatusError as e:
            message = f"Anthropic API error: {e.status_code} {e.message}"
            raise LLMError(message, retryable=transient_status(e.status_code)) from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"Anthropic connection error: {e}") from e

        result = self._parse_response(response)
        result.provider = self._provider_name
        result.request_model = kwargs["model"]
        result.wire = wire_tag(self._provider_name, kwargs["model"])
        if (billed := _compaction_counts(response.usage)) is not None:
            # No reply was generated: the top-level counts are 0, the call is in `usage.iterations`
            result.input_tokens = billed["input_tokens"]
            result.cache_write_5m_tokens = billed["cache_write_5m_tokens"]
            result.cache_write_1h_tokens = billed["cache_write_1h_tokens"]
            result.cache_read_tokens = billed["cache_read_tokens"]
            result.output_tokens = billed["output_tokens"]
        return result

    async def close(self) -> None:
        await self._client.close()

    # ── Internal ─────────────────────────────────────────

    def _messages_api(self, kwargs: dict[str, Any]) -> Any:
        """The SDK's Messages namespace for a request: the beta one when it carries betas."""
        client = self._request_clients.pick(self._client)
        return client.beta.messages if "betas" in kwargs else client.messages

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
                async with self._messages_api(kwargs).stream(**kwargs) as stream:
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

    def _convert_messages(self, messages: list[LLMMessage], model: str) -> list[dict[str, Any]]:
        """Convert unified messages to Anthropic API format.

        An assistant message goes back as the provider's own output (thinking included) while it
        was produced by this provider and model (see `llm.replay`); any other is rebuilt from its
        text and tool calls.
        """
        wire = wire_tag(self._provider_name, model)
        result: list[dict[str, Any]] = []
        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                # A server compaction's block goes back as the API returned it, as an assistant message of
                # its own, while this wire produced it
                replayed = replayable_raw(msg, wire)
                if replayed is not None:
                    result.append({"role": "assistant", "content": replayed})
                    continue
                # A history summary (the system prompt is the `system` parameter): sent as a
                # user text block of its own, merged with the next user turn below
                result.append({"role": "user", "content": [{"type": "text", "text": msg.content}]})
                continue

            if msg.role == MessageRole.TOOL_RESULT:
                block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": msg.tool_call_id,
                    "content": msg.content,
                }
                if msg.is_error:
                    block["is_error"] = True
                result.append({"role": "user", "content": [block]})
                continue

            if msg.role == MessageRole.ASSISTANT:
                replayed = replayable_raw(msg, wire)
                if replayed is not None and _tool_use_ids(replayed) != [tc.id for tc in msg.tool_calls]:
                    replayed = None  # edited since its response (pair repair dropped a call): rebuilt instead
                # Thinking with nothing after it is not sent: an assistant message holding
                # only thinking is never valid
                if replayed is not None and not all(_is_thinking(b) for b in replayed):
                    result.append({"role": "assistant", "content": replayed})
                    continue
                if msg.tool_calls:
                    content_blocks: list[dict[str, Any]] = []
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
    def _validate_api_messages(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Enforce Anthropic constraints on the converted message list.

        Rules:
        1. Every assistant message containing tool_use blocks must be
           immediately followed by user message(s) with matching tool_result
           blocks for ALL tool_use IDs.
        2. Consecutive same-role messages are merged (Anthropic rejects them).
        3. The first non-system message must be role=user (a server compaction block excepted).
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
            if merged and merged[-1]["role"] == msg["role"] and not _has_compaction(merged[-1]):
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

        # Pass 4: ensure first message is role=user (the compaction block goes first, as the API wants it)
        if merged and merged[0]["role"] != "user" and not _has_compaction(merged[0]):
            merged.insert(0, {"role": "user", "content": "(continuing conversation)"})

        return merged

    def _parse_response(self, response: Message | BetaMessage) -> LLMResponse:
        """Parse an Anthropic Message (or beta Message) into unified LLMResponse."""
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        raw: list[dict[str, Any]] = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
                raw.append({"type": "text", "text": block.text})
            elif block.type == "thinking":
                raw.append({"type": "thinking", "thinking": block.thinking, "signature": block.signature})
            elif block.type == "redacted_thinking":
                raw.append({"type": "redacted_thinking", "data": block.data})
            elif block.type == "tool_use":
                call = ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=dict(block.input) if isinstance(block.input, dict) else {},
                )
                tool_calls.append(call)
                raw.append({"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments})
            else:  # a block type Pincer does not read, kept as the API sent it
                raw.append(block.model_dump(mode="json", exclude_none=True))

        return LLMResponse(
            content="\n".join(text_parts),
            tool_calls=tool_calls,
            model=response.model,
            stop_reason=response.stop_reason or "",
            **_usage_counts(response.usage),
            raw=raw,
            thinking_dropped=_thinking_dropped(response),
        )


def _thinking_dropped(response: Message | BetaMessage) -> list[ThinkingDrop]:
    """The thinking blocks the API reports it dropped from the request (`input_transformations`,
    an extra field of the beta Message). Entries of another type are not Pincer's to read."""
    extra = response.model_extra
    entries = (extra.get("input_transformations") if isinstance(extra, dict) else None) or []
    return [
        ThinkingDrop(path=str(entry.get("path", "")), reason=str(entry.get("reason", "")))
        for entry in entries
        if isinstance(entry, dict) and entry.get("type") == "thinking_dropped"
    ]


def _usage_counts(usage: Usage | BetaUsage | BetaCompactionIterationUsage) -> TokenCounts:
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


def _compaction_counts(usage: Usage | BetaUsage) -> TokenCounts | None:
    """What a compaction call was billed: the sum of the `compaction` entries of `usage.iterations`
    (the beta usage reports them; the top-level counts are 0). None when it reports none."""
    entries = [e for e in getattr(usage, "iterations", None) or [] if e.type == "compaction"]
    if not entries:
        return None
    counts = [_usage_counts(entry) for entry in entries]
    return TokenCounts(
        input_tokens=sum(c["input_tokens"] for c in counts),
        cache_write_5m_tokens=sum(c["cache_write_5m_tokens"] for c in counts),
        cache_write_1h_tokens=sum(c["cache_write_1h_tokens"] for c in counts),
        cache_read_tokens=sum(c["cache_read_tokens"] for c in counts),
        output_tokens=sum(c["output_tokens"] for c in counts),
    )


def _tool_use_ids(blocks: list[dict[str, Any]]) -> list[str]:
    return [b["id"] for b in blocks if b.get("type") == "tool_use"]


def _has_compaction(message: dict[str, Any]) -> bool:
    """A message that carries a server compaction block, which the API takes as a message of its own."""
    content = message.get("content")
    return isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "compaction" for b in content)


def _is_thinking(block: Any) -> bool:
    return isinstance(block, dict) and block.get("type") in _THINKING_TYPES
