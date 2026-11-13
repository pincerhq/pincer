"""
Shared helpers and base class for OpenAI-compatible providers.

Helpers: convert_messages_to_openai, convert_tools_to_openai, parse_openai_response.
Base class: OpenAICompatibleProvider — reads all config from Settings directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import aclosing
from typing import TYPE_CHECKING, Any

import openai
from openai import AsyncOpenAI

from pincer.exceptions import LLMError, LLMRateLimitError
from pincer.llm.base import (
    STOP_REASON_MAX_TOKENS,
    BaseLLMProvider,
    LLMMessage,
    LLMResponse,
    MessageRole,
    RequestClients,
    StreamTurnEvent,
    TokenCounts,
    ToolCall,
    prompt_cache_key,
    rate_limit_attempts,
    rate_limit_backoff,
    transient_status,
)
from pincer.llm.capabilities import endpoint_capabilities
from pincer.llm.model_params import ReasoningWire, openai_is_reasoning_model, resolve_and_warn
from pincer.llm.openai_responses import (
    convert_messages_to_responses,
    convert_tools_to_responses,
    parse_responses_response,
)
from pincer.llm.replay import wire_tag

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

    from openai.types.chat import ChatCompletion
    from openai.types.responses import Response

    from pincer.config import Settings
    from pincer.config.llm import ReasoningEffort, ThinkingMode

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────


# OpenAI rejects requests with more than 128 tools (error 400,
# array_above_max_length) — Anthropic has no such cap, so callers sized for
# the full registry must be truncated here rather than fail every request.
OPENAI_MAX_TOOLS = 128
# OpenAI's finish_reason when the output hit the token limit
_FINISH_REASON_LENGTH = "length"
# Asks a Responses request to return its reasoning items encrypted, so they can be sent back (store=false)
_INCLUDE_ENCRYPTED_REASONING = "reasoning.encrypted_content"
# Stream events that end a Responses reply with its whole `response` (a failed one raises instead)
_ENDING_EVENTS = ("response.completed", "response.incomplete")
# Error kinds of a failure inside a Responses stream (an HTTP 200 whose events then say it failed):
# OpenAI's `rate_limit_exceeded` and `server_error` response codes, Azure's `too_many_requests` error type
_RATE_LIMIT_KINDS = {"rate_limit_exceeded", "too_many_requests"}
_SERVER_ERROR_KINDS = {"server_error"}
# The first wait after a 429, which the SDK's error carries no retry-after for here; it doubles per retry
_RATE_LIMIT_WAIT_S = 5.0


def cap_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The first ``OPENAI_MAX_TOOLS`` tools; the rest are named in a warning."""
    if len(tools) > OPENAI_MAX_TOOLS:
        logger.warning(
            "%d tools exceed OpenAI's %d-tool limit — sending the first %d and dropping: %s",
            len(tools),
            OPENAI_MAX_TOOLS,
            OPENAI_MAX_TOOLS,
            ", ".join(t.get("name", "?") for t in tools[OPENAI_MAX_TOOLS:]),
        )
        tools = tools[:OPENAI_MAX_TOOLS]
    return tools


def convert_tools_to_openai(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Anthropic-style tool defs to OpenAI function-calling format."""
    tools = cap_tools(tools)
    oai_tools: list[dict[str, Any]] = []
    for tool in tools:
        oai_tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                },
            }
        )
    return oai_tools


def convert_messages_to_openai(
    messages: list[LLMMessage],
    system: str | None = None,
) -> list[dict[str, Any]]:
    """Convert unified LLMMessage list to OpenAI API format."""
    result: list[dict[str, Any]] = []
    if system:
        result.append({"role": "system", "content": system})

    for msg in messages:
        if msg.role == MessageRole.SYSTEM:
            result.append({"role": "system", "content": msg.content})
        elif msg.role == MessageRole.TOOL_RESULT:
            result.append(
                {
                    "role": "tool",
                    "tool_call_id": msg.tool_call_id or "",
                    "content": msg.content,
                }
            )
        elif msg.role == MessageRole.ASSISTANT and msg.tool_calls:
            tool_calls_api = []
            for tc in msg.tool_calls:
                tool_calls_api.append(
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                )
            result.append(
                {
                    "role": "assistant",
                    "content": msg.content or None,
                    "tool_calls": tool_calls_api,
                }
            )
        elif msg.images or msg.context:
            # Pincer's context block first, then the images, then the user's words
            content_parts: list[dict[str, Any]] = []
            if msg.context:
                content_parts.append({"type": "text", "text": msg.context})
            for img in msg.images:
                content_parts.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{img.media_type};base64,{img.data}",
                        },
                    }
                )
            if msg.content:
                content_parts.append({"type": "text", "text": msg.content})
            result.append({"role": msg.role.value, "content": content_parts})
        else:
            result.append({"role": msg.role.value, "content": msg.content})

    return result


def _normalize_finish_reason(finish_reason: str | None) -> str:
    """Report OpenAI's ``length`` as ``max_tokens``, like Anthropic, so one
    truncation check covers both wires; other reasons pass through."""
    if finish_reason == _FINISH_REASON_LENGTH:
        return STOP_REASON_MAX_TOKENS
    return finish_reason or ""


def _token_count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _usage_counts(usage: Any) -> TokenCounts:
    """The token counts of an OpenAI `usage` (None when the API sent none).

    `prompt_tokens` is the whole prompt: the cached part (`cached_tokens`) and, where the
    API reports it (Azure, gpt-6), the part written to the cache (`cache_write_tokens`,
    stored as 5-minute writes) come out of it, leaving uncached input.
    """
    prompt = _token_count(getattr(usage, "prompt_tokens", 0))
    details = getattr(usage, "prompt_tokens_details", None)
    read = _token_count(getattr(details, "cached_tokens", 0))
    # Not in the SDK's model: read from the response's extra fields
    written = _token_count(getattr(details, "cache_write_tokens", 0))
    return TokenCounts(
        input_tokens=max(0, prompt - read - written),
        cache_write_5m_tokens=written,
        cache_write_1h_tokens=0,
        cache_read_tokens=read,
        output_tokens=_token_count(getattr(usage, "completion_tokens", 0)),
    )


def _responses_usage_counts(usage: Any) -> TokenCounts:
    """The token counts of a Responses `usage` (None when the API sent none).

    `input_tokens` is the whole prompt: the cached part (`cached_tokens`, billed reads) and the part
    written to the cache (`cache_write_tokens`, stored as 5-minute writes) come out of it. `output_tokens`
    includes the reasoning tokens, which are billed as output.
    """
    total = _token_count(getattr(usage, "input_tokens", 0))
    details = getattr(usage, "input_tokens_details", None)
    read = _token_count(getattr(details, "cached_tokens", 0))
    # Not in the SDK's model: read from the response's extra fields
    written = _token_count(getattr(details, "cache_write_tokens", 0))
    return TokenCounts(
        input_tokens=max(0, total - read - written),
        cache_write_5m_tokens=written,
        cache_write_1h_tokens=0,
        cache_read_tokens=read,
        output_tokens=_token_count(getattr(usage, "output_tokens", 0)),
    )


def parse_openai_response(response: ChatCompletion) -> LLMResponse:
    """Parse OpenAI ChatCompletion into unified LLMResponse."""
    choice = response.choices[0]
    message = choice.message
    tool_calls: list[ToolCall] = []

    if message.tool_calls:
        for tc in message.tool_calls:
            if tc.type != "function":  # skip non-function (custom) tool calls
                continue
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))

    return LLMResponse(
        content=message.content or "",
        tool_calls=tool_calls,
        model=response.model,
        stop_reason=_normalize_finish_reason(choice.finish_reason),
        **_usage_counts(response.usage),
    )


class _StreamFailed(Exception):
    """A Responses stream that failed after it started: the `error` event or a `response.failed` event."""

    def __init__(self, code: str | None, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.type = None
        self.message = message


def _stream_failure(error: openai.APIError | _StreamFailed) -> tuple[bool, bool, str]:
    """(is it a rate limit, is it worth asking again, what to say) for a failed stream request: an HTTP
    error, a failure event, or the SDK's error for an error payload (Azure's shape)."""
    if isinstance(error, openai.APIStatusError):
        return error.status_code == 429, transient_status(error.status_code), f"{error.status_code} {error.message}"
    kinds = {error.code, error.type}
    return bool(kinds & _RATE_LIMIT_KINDS), bool(kinds & _SERVER_ERROR_KINDS), error.message


# ── Base class ────────────────────────────────────────────────────────────────


class OpenAICompatibleProvider(BaseLLMProvider):
    """Provider for any OpenAI-wire endpoint.

    `provider == "openai"` uses the well-known API (key + built-in base URL); any
    other name uses the configured compatible endpoint (base URL + optional key).
    The model id is passed straight through — no claude→model mapping.
    """

    def __init__(self, settings: Settings, provider: str) -> None:
        self._provider_name = provider

        if provider == "openai":
            api_key = settings.openai_api_key.get_secret_value()
            if not api_key:
                raise ValueError("provider 'openai' needs PINCER_OPENAI_API_KEY")
            base_url = settings.openai_base_url
            model = settings.openai_model or settings.default_model
        else:
            api_key = settings.openai_compatible_api_key.get_secret_value() or "none"
            base_url = settings.openai_compatible_base_url
            model = settings.openai_compatible_model or settings.default_model

        # An OpenAI-wire endpoint won't accept an Anthropic-native model id. This
        # catches PINCER_DEFAULT_MODEL (a claude-* default) leaking onto an OpenAI
        # provider with no model of its own — fail fast instead of a runtime 404.
        if model.startswith("claude"):
            env = "PINCER_OPENAI_MODEL" if provider == "openai" else "PINCER_OPENAI_COMPATIBLE_MODEL"
            raise ValueError(
                f"OpenAI-wire provider {provider!r} resolved to model {model!r}, which an OpenAI "
                f"endpoint will reject. Set {env} to a model this endpoint supports."
            )

        from pincer.llm.router import is_free_provider  # the router imports this module

        # Strict endpoints reject the unknown field (a 400 on every request), and a keyless local
        # endpoint has no paid cache to key: only the providers listed as accepting it get a key
        self._sends_cache_key = provider in settings.prompt_cache_key_providers and not is_free_provider(
            settings, provider
        )
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=2)
        self._request_clients = RequestClients[AsyncOpenAI]()
        # Chat Completions (today's code) or the Responses API for this endpoint, read once
        self._responses_api = endpoint_capabilities(settings, provider).responses_api
        self._default_model = model
        self._default_max_tokens = settings.max_tokens
        self._default_temperature = settings.temperature
        # Read once: every request of a turn sends the same effort/thinking
        self._reasoning_effort = settings.reasoning_effort
        self._thinking = settings.thinking

    # OpenAI reasoning models reject `max_tokens` (want `max_completion_tokens`)
    # and any non-default temperature.
    def _normalize_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        model = str(kwargs.get("model", ""))
        if openai_is_reasoning_model(model):
            kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
            kwargs.pop("temperature", None)
            logger.debug("Dropping temperature: %s does not accept it", model)
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
            "temperature": temperature if temperature is not None else self._default_temperature,
            "messages": convert_messages_to_openai(messages, system),
        }
        if tools:
            kwargs["tools"] = convert_tools_to_openai(tools)
        if self._sends_cache_key and (cache_key := prompt_cache_key()):
            # One key per conversation routes its requests to the same cache (issue #357, Q21)
            kwargs["prompt_cache_key"] = cache_key
        # A per-call value (voice, summary) replaces the global setting; None keeps it.
        reasoning = resolve_and_warn(
            ReasoningWire.OPENAI,
            model,
            self._reasoning_effort if reasoning_effort is None else reasoning_effort,
            self._thinking if thinking is None else thinking,
            pinned=pinned,
            with_tools=bool(tools),
        )
        kwargs.update(reasoning.fragment)
        return self._normalize_kwargs(kwargs)

    def _build_responses_kwargs(
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
        """Request kwargs of a Responses call: stateless (`store` false), reasoning items encrypted."""
        model = model or self._default_model
        kwargs: dict[str, Any] = {
            "model": model,
            "input": convert_messages_to_responses(messages, wire_tag(self._provider_name, model)),
            "store": False,
            "max_output_tokens": max_tokens or self._default_max_tokens,
        }
        if system:
            kwargs["instructions"] = system
        if tools:
            kwargs["tools"] = convert_tools_to_responses(cap_tools(tools))
        if openai_is_reasoning_model(model):
            kwargs["include"] = [_INCLUDE_ENCRYPTED_REASONING]
        else:  # a reasoning model rejects a temperature
            kwargs["temperature"] = temperature if temperature is not None else self._default_temperature
        if self._sends_cache_key and (cache_key := prompt_cache_key()):
            kwargs["prompt_cache_key"] = cache_key
        # The same effort rules as Chat Completions; the Responses API takes the level as `reasoning.effort`
        reasoning = resolve_and_warn(
            ReasoningWire.OPENAI,
            model,
            self._reasoning_effort if reasoning_effort is None else reasoning_effort,
            self._thinking if thinking is None else thinking,
            pinned=pinned,
            with_tools=bool(tools),
        )
        if effort := reasoning.fragment.get("reasoning_effort"):
            kwargs["reasoning"] = {"effort": effort}
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
        build = self._build_responses_kwargs if self._responses_api else self._build_kwargs
        kwargs = build(
            messages,
            tools,
            model,
            max_tokens,
            temperature,
            system,
            reasoning_effort=reasoning_effort,
            thinking=thinking,
            pinned=pinned,
        )

        try:
            response = await self._call_with_retry(kwargs, responses=self._responses_api)
        except openai.APIStatusError as e:
            message = f"{self._provider_name} API error: {e.status_code} {e.message}"
            raise LLMError(message, retryable=transient_status(e.status_code)) from e
        except openai.APIConnectionError as e:
            raise LLMError(f"{self._provider_name} connection error: {e}") from e

        if self._responses_api:
            result = self._parse_responses(response, kwargs["model"])
        else:
            result = parse_openai_response(response)
        result.provider = self._provider_name
        result.request_model = kwargs["model"]
        return result

    def _parse_responses(self, response: Response, model: str) -> LLMResponse:
        """A Responses reply as a unified response, with its usage and a log line about its reasoning."""
        result = parse_responses_response(response, wire_tag(self._provider_name, model))
        counts = _responses_usage_counts(response.usage)
        result.input_tokens = counts["input_tokens"]
        result.cache_write_5m_tokens = counts["cache_write_5m_tokens"]
        result.cache_read_tokens = counts["cache_read_tokens"]
        result.output_tokens = counts["output_tokens"]
        self._log_reasoning(response, model)
        return result

    def _log_reasoning(self, response: Response, model: str) -> None:
        """How much of the output was reasoning, and which reasoning mode the API says it used
        (`current_turn`: earlier turns' reasoning is not rendered). Counts and a mode name only."""
        details = getattr(response.usage, "output_tokens_details", None)
        reasoning = getattr(response.reasoning, "context", None) if response.reasoning else None
        logger.debug(
            "%s %s: %d output tokens, of which %d reasoning; reasoning context %s",
            self._provider_name,
            model,
            _token_count(getattr(response.usage, "output_tokens", 0)),
            _token_count(getattr(details, "reasoning_tokens", 0)),
            reasoning or "unreported",
        )

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
        """True single-generation streaming (Sprint 5): content deltas are
        yielded as they arrive; tool-call deltas are assembled across chunks
        and returned with usage in the final event — same contract as the
        Anthropic implementation, so the voice pipeline can run either.

        A 429 before the first event is retried as `complete()` retries it; once
        text has been yielded nothing is retried, since the caller already has it.
        A listed endpoint streams on the Responses API (``_stream_responses``) and gives the
        caller the same events."""
        if self._responses_api:
            kwargs = self._build_responses_kwargs(
                messages,
                tools,
                model,
                max_tokens,
                temperature,
                system,
                reasoning_effort=reasoning_effort,
                thinking=thinking,
            )
            kwargs["stream"] = True
            # aclosing: a caller that stops reading closes the request's stream too, at once
            async with aclosing(self._stream_responses(kwargs)) as events:
                async for event in events:
                    yield event
            return
        kwargs = self._build_kwargs(
            messages,
            tools,
            model,
            max_tokens,
            temperature,
            system,
            reasoning_effort=reasoning_effort,
            thinking=thinking,
        )
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}

        attempts = rate_limit_attempts()
        for attempt in range(attempts):
            started = False
            text_parts: list[str] = []
            tool_accum: dict[int, dict[str, str]] = {}
            finish_reason = ""
            model_name = ""
            usage: Any = None
            try:
                stream = await self._request_clients.pick(self._client).chat.completions.create(**kwargs)
                async for chunk in stream:
                    if getattr(chunk, "usage", None):
                        usage = chunk.usage
                    model_name = getattr(chunk, "model", "") or model_name
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    delta = choice.delta
                    if delta and delta.content:
                        text_parts.append(delta.content)
                        started = True
                        yield StreamTurnEvent(text=delta.content)
                    if delta and delta.tool_calls:
                        for tc in delta.tool_calls:
                            slot = tool_accum.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                            if tc.id:
                                slot["id"] = tc.id
                            if tc.function and tc.function.name:
                                slot["name"] += tc.function.name
                            if tc.function and tc.function.arguments:
                                slot["args"] += tc.function.arguments
                    if choice.finish_reason:
                        finish_reason = choice.finish_reason
                break
            except openai.RateLimitError as e:
                await self._wait_after_rate_limit(e, started=started, attempt=attempt, attempts=attempts)
            except openai.APIStatusError as e:
                message = f"{self._provider_name} stream error: {e.status_code} {e.message}"
                raise LLMError(message, retryable=transient_status(e.status_code)) from e
            except openai.APIConnectionError as e:
                raise LLMError(f"{self._provider_name} connection error: {e}") from e

        tool_calls: list[ToolCall] = []
        for index in sorted(tool_accum):
            slot = tool_accum[index]
            try:
                arguments = json.loads(slot["args"]) if slot["args"] else {}
            except json.JSONDecodeError:
                logger.warning("Unparseable streamed tool arguments for %s — using {}", slot["name"])
                arguments = {}
            tool_calls.append(ToolCall(id=slot["id"] or f"call_{index}", name=slot["name"], arguments=arguments))

        yield StreamTurnEvent(
            response=LLMResponse(
                content="".join(text_parts),
                tool_calls=tool_calls,
                model=model_name,
                provider=self._provider_name,
                request_model=kwargs["model"],
                stop_reason=_normalize_finish_reason(finish_reason),
                **_usage_counts(usage),
            )
        )

    async def _stream_responses(self, kwargs: dict[str, Any]) -> AsyncGenerator[StreamTurnEvent]:
        """One streamed Responses request: the text deltas as they arrive, then the final response.

        The final response is built from the items of the `response.output_item.done` events laid over
        the terminal event's own output: the docs give the complete reasoning item (its
        `encrypted_content` included) only there. The terminal event can be `response.incomplete`
        (the output limit), which the SDK's `get_final_response` would turn into an error.
        """
        attempts = rate_limit_attempts()
        for attempt in range(attempts):
            started = False
            done: dict[int, Any] = {}
            terminal: Response | None = None
            try:
                stream = await self._request_clients.pick(self._client).responses.create(**kwargs)
                async with stream:  # closes the HTTP stream when the caller stops reading
                    async for event in stream:
                        if event.type == "response.output_text.delta":
                            if event.delta:
                                started = True
                                yield StreamTurnEvent(text=event.delta)
                        elif event.type == "response.output_item.done":
                            done[event.output_index] = event.item
                        elif event.type in _ENDING_EVENTS:
                            terminal = event.response  # type: ignore[union-attr]
                        elif event.type == "response.failed":
                            failure = event.response.error
                            raise _StreamFailed(failure.code if failure else None, failure.message if failure else "")
                        elif event.type == "error":
                            raise _StreamFailed(event.code, event.message)
                break
            except openai.APIConnectionError as e:
                raise LLMError(f"{self._provider_name} connection error: {e}") from e
            except (openai.APIError, _StreamFailed) as e:
                rate_limited, retryable, detail = _stream_failure(e)
                if not rate_limited:
                    raise LLMError(f"{self._provider_name} stream error: {detail}", retryable=retryable) from e
                await self._wait_after_rate_limit(e, started=started, attempt=attempt, attempts=attempts)

        if terminal is None:
            raise LLMError(f"{self._provider_name} stream ended without a final response", retryable=True)
        items = dict(enumerate(terminal.output))
        items.update(done)
        response = terminal.model_copy(update={"output": [items[index] for index in sorted(items)]})
        result = self._parse_responses(response, kwargs["model"])
        result.provider = self._provider_name
        result.request_model = kwargs["model"]
        yield StreamTurnEvent(response=result)

    async def _wait_after_rate_limit(self, error: BaseException, *, started: bool, attempt: int, attempts: int) -> None:
        """After a rate limit of a streamed request: wait before the next attempt, or raise when none is
        left or text has already been yielded (the caller has it, so nothing is retried)."""
        if started or attempt == attempts - 1:
            raise LLMRateLimitError(retry_after=_RATE_LIMIT_WAIT_S) from error
        wait = rate_limit_backoff(_RATE_LIMIT_WAIT_S, attempt)
        logger.warning("%s rate limited, retrying in %.1fs", self._provider_name, wait)
        await asyncio.sleep(wait)

    async def close(self) -> None:
        await self._client.close()

    async def _call_with_retry(self, kwargs: dict[str, Any], *, responses: bool = False) -> Any:
        """One request, with the 429 retries; the Responses API when ``responses``, else Chat Completions."""
        attempts = rate_limit_attempts()
        for attempt in range(attempts):
            try:
                client = self._request_clients.pick(self._client)
                if responses:
                    return await client.responses.create(**kwargs)
                return await client.chat.completions.create(**kwargs)
            except openai.RateLimitError as e:
                if attempt == attempts - 1:
                    raise LLMRateLimitError(retry_after=_RATE_LIMIT_WAIT_S) from e
                wait = rate_limit_backoff(_RATE_LIMIT_WAIT_S, attempt)
                logger.warning("%s rate limited, retrying in %.1fs", self._provider_name, wait)
                await asyncio.sleep(wait)
        raise LLMError("Exhausted retries")
