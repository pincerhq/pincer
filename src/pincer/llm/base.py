"""
Abstract LLM provider interface and unified message types.

All providers must implement this interface so the Agent core
never depends on Anthropic/OpenAI specifics.
"""

from __future__ import annotations

import base64
import weakref
from abc import ABC, abstractmethod
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, Protocol, Self, TypedDict

from pincer.config.pricing import TokenUsage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

    from pincer.config.llm import ReasoningEffort, ThinkingMode

# The stop reason every provider reports when the output hit max_tokens
STOP_REASON_MAX_TOKENS = "max_tokens"
# A rate-limited (429) request is sent this many times in all, by complete() and by stream_turn()
# before its first event; the wait doubles each time, up to the cap
RATE_LIMIT_ATTEMPTS = 3
_RATE_LIMIT_MAX_WAIT_S = 60.0
# False while the router sends a request that fails over on a 429 (Q23: fail over first): set by
# the router around one provider call, read by the providers before their first request
_rate_limit_retries: ContextVar[bool] = ContextVar("pincer_rate_limit_retries", default=True)
# An SDK client → its copy without the SDK's own retries, made once; keyed on the client object,
# so a replaced client gets its own copy and a dropped one takes its copy with it
_no_retry_clients: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()
# Where the requests of the current conversation get their prompt cache key (issue #357, Q21):
# set by the agent around a turn, read by providers that route on it (OpenAI's prompt_cache_key)
_prompt_cache_key_source: ContextVar[Callable[[], str] | None] = ContextVar("pincer_prompt_cache_key", default=None)


@contextmanager
def prompt_cache_session(key: Callable[[], str]) -> Iterator[None]:
    """Requests made inside get the conversation's cache key from ``key``, called only when a
    provider asks for it (so a key is minted only for a provider that sends one)."""
    token = _prompt_cache_key_source.set(key)
    try:
        yield
    finally:
        try:
            _prompt_cache_key_source.reset(token)
        except ValueError:  # an async generator closed from another context: just clear it there
            _prompt_cache_key_source.set(None)


def prompt_cache_key() -> str:
    """The current conversation's prompt cache key; "" outside a conversation."""
    source = _prompt_cache_key_source.get()
    return source() if source is not None else ""


@contextmanager
def rate_limit_retries(enabled: bool) -> Iterator[None]:
    """Requests made inside retry a 429 (``enabled``) or raise it at once, SDK retries included,
    so the router can fail over without waiting. Never hold it across a yield: the consumer of
    a stream would run with it set."""
    token = _rate_limit_retries.set(enabled)
    try:
        yield
    finally:
        _rate_limit_retries.reset(token)


def rate_limit_attempts() -> int:
    """How many times a provider sends a rate-limited request: ``RATE_LIMIT_ATTEMPTS``, or 1
    inside ``rate_limit_retries(False)``."""
    return RATE_LIMIT_ATTEMPTS if _rate_limit_retries.get() else 1


class _SDKClient(Protocol):
    """What ``request_client`` needs of an SDK client (AsyncAnthropic, AsyncOpenAI)."""

    def with_options(self, *, max_retries: int) -> Self: ...


def request_client[C: _SDKClient](client: C) -> C:
    """The client to send one request with: ``client``, or, inside ``rate_limit_retries(False)``,
    its copy without the SDK's own retries, so a 429 fails over at once. The copy is made once
    per client and shares the client's HTTP connection pool."""
    if rate_limit_attempts() > 1:
        return client
    copy: C | None = _no_retry_clients.get(client)
    if copy is None:
        copy = _no_retry_clients[client] = client.with_options(max_retries=0)
    return copy


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_RESULT = "tool_result"


@dataclass(frozen=True, slots=True)
class ImageContent:
    """Base64-encoded image to send to the LLM."""

    data: str  # base64 string
    media_type: str  # e.g. "image/jpeg"

    @classmethod
    def from_bytes(cls, raw: bytes, media_type: str) -> ImageContent:
        return cls(data=base64.b64encode(raw).decode(), media_type=media_type)


@dataclass(frozen=True, slots=True)
class ThinkingBlock:
    """One thinking or redacted-thinking block of a response, verbatim.

    Kept only within the agent turn that produced it, and never saved, shown
    or logged: the content fields stay out of the repr.
    """

    kind: Literal["thinking", "redacted_thinking"]
    thinking: str = field(default="", repr=False)  # kind == "thinking"
    signature: str = field(default="", repr=False)  # kind == "thinking"
    data: str = field(default="", repr=False)  # kind == "redacted_thinking" (encrypted)


@dataclass(slots=True)
class LLMMessage:
    """Unified message format for all providers."""

    role: MessageRole
    content: str = ""
    images: list[ImageContent] = field(default_factory=list)
    tool_call_id: str | None = None  # For tool_result messages
    tool_calls: list[ToolCall] = field(default_factory=list)  # From assistant
    # The response's thinking, sent back on the turn's later requests; never in to_dict
    thinking_blocks: list[ThinkingBlock] = field(default_factory=list, repr=False)
    # The response's content blocks as the API sent them, in order (thinking, text, tool_use
    # interleaved). Set only when the response had thinking, so the next request can replay it
    # as it was; never in to_dict.
    content_blocks: list[dict[str, Any]] = field(default_factory=list, repr=False)
    # Pincer's per-turn context for a user message (recalled memories, the current time), kept out
    # of the system prompt so that stays identical for the whole conversation. Sent as its own text
    # block before the user's words, saved with the session and replayed as it was; never shown
    # to people as history (the user's words are `content`).
    context: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Minimal dict for session storage (JSON-serializable)."""
        d: dict[str, Any] = {"role": self.role.value, "content": self.content}
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            d["tool_calls"] = [tc.to_dict() for tc in self.tool_calls]
        if self.context:
            d["context"] = self.context
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LLMMessage:
        tool_calls = [ToolCall.from_dict(tc) for tc in d.get("tool_calls", [])]
        return cls(
            role=MessageRole(d["role"]),
            content=d.get("content", ""),
            tool_call_id=d.get("tool_call_id"),
            tool_calls=tool_calls,
            context=d.get("context", ""),
        )


@dataclass(frozen=True, slots=True)
class ToolCall:
    """An LLM's request to invoke a tool."""

    id: str
    name: str
    arguments: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ToolCall:
        return cls(id=d["id"], name=d["name"], arguments=d["arguments"])


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Result of a tool execution, to be sent back to the LLM."""

    tool_call_id: str
    content: str
    is_error: bool = False


class TokenCounts(TypedDict):
    """A provider's usage as `LLMResponse` keyword arguments (each leaf parses its own)."""

    input_tokens: int  # uncached
    cache_write_5m_tokens: int
    cache_write_1h_tokens: int
    cache_read_tokens: int
    output_tokens: int


@dataclass(slots=True)
class LLMResponse:
    """Unified response from any LLM provider."""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    provider: str = ""  # name of the provider that actually served this response
    # The model name Pincer sent, which prices the call; `model` is what the API reports
    # back (a dated snapshot, or the model behind an Azure deployment name)
    request_model: str = ""
    input_tokens: int = 0  # uncached input only; the cache counts below are the rest of the prompt
    output_tokens: int = 0
    stop_reason: str = ""
    # Prompt-cache usage: 0 when the provider doesn't report it
    cache_read_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    thinking_blocks: list[ThinkingBlock] = field(default_factory=list, repr=False)
    # Every content block in the order the API sent them; filled only when there is thinking
    content_blocks: list[dict[str, Any]] = field(default_factory=list, repr=False)

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0

    @property
    def usage(self) -> TokenUsage:
        """Every token type of this call, as it is priced and recorded."""
        return TokenUsage(
            input_tokens=self.input_tokens,
            cache_write_5m_tokens=self.cache_write_5m_tokens,
            cache_write_1h_tokens=self.cache_write_1h_tokens,
            cache_read_tokens=self.cache_read_tokens,
            output_tokens=self.output_tokens,
        )

    @property
    def truncated(self) -> bool:
        """The output stopped on max_tokens: its last block may be incomplete."""
        return self.stop_reason == STOP_REASON_MAX_TOKENS


def transient_status(status_code: int) -> bool:
    """An HTTP status worth asking again (Q24a): a rate limit (429), an overload (529) or another
    server error (5xx). A 4xx is the request's own fault and would fail the same way."""
    return status_code == 429 or status_code >= 500


def rate_limit_backoff(retry_after: float, attempt: int) -> float:
    """Seconds to wait after the `attempt`-th (0-based) 429: retry_after·2^attempt, at most 60 s."""
    return min(retry_after * 2.0**attempt, _RATE_LIMIT_MAX_WAIT_S)


def strip_thinking(messages: list[LLMMessage]) -> None:
    """Drop every message's thinking blocks, in place (end of an agent turn)."""
    for message in messages:
        if message.thinking_blocks:
            message.thinking_blocks = []
            message.content_blocks = []


@dataclass(frozen=True, slots=True)
class StreamTurnEvent:
    """One event from ``BaseLLMProvider.stream_turn``: a text delta while the
    model generates, or (exactly once, last) the complete LLMResponse."""

    text: str = ""
    response: LLMResponse | None = None


class BaseLLMProvider(ABC):
    """Abstract interface every LLM provider must implement."""

    @abstractmethod
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
        """Send messages and get a complete response.

        ``pinned`` marks an internal structured call whose effort and thinking are fixed by
        Pincer, not the operator's: a value the model can't take is then logged at DEBUG only.
        """
        ...

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
        """Stream ONE turn: text tokens as they arrive, then the complete
        LLMResponse (with tool calls and usage) as the final event.

        Unlike ``complete()`` the caller sees text before generation finishes, and it
        never loses tool calls: the voice latency pipeline (Sprint 5) and the chat
        reply both rely on that.

        ``reasoning_effort`` and ``thinking`` (on every method) override the
        global settings for this call; ``None`` means the global setting.
        Providers translate them per model and drop what a model can't take.

        Default implementation falls back to ``complete()`` (one text event
        with the full content, then the response) so every provider works;
        providers with true incremental APIs override it.
        """
        response = await self.complete(
            messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            reasoning_effort=reasoning_effort,
            thinking=thinking,
        )
        if response.content:
            yield StreamTurnEvent(text=response.content)
        yield StreamTurnEvent(response=response)

    @abstractmethod
    async def close(self) -> None:
        """Clean up resources (HTTP clients, etc.)."""
        ...
