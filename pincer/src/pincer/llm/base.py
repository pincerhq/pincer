"""
Abstract LLM provider interface and unified message types.

All providers must implement this interface so the Agent core
never depends on Anthropic/OpenAI specifics.
"""

from __future__ import annotations

import base64
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


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


@dataclass(slots=True)
class LLMMessage:
    """Unified message format for all providers."""

    role: MessageRole
    content: str = ""
    images: list[ImageContent] = field(default_factory=list)
    tool_call_id: str | None = None  # For tool_result messages
    tool_calls: list[ToolCall] = field(default_factory=list)  # From assistant

    def to_dict(self) -> dict[str, Any]:
        """Minimal dict for session storage (JSON-serializable)."""
        d: dict[str, Any] = {"role": self.role.value, "content": self.content}
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            d["tool_calls"] = [tc.to_dict() for tc in self.tool_calls]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LLMMessage:
        tool_calls = [ToolCall.from_dict(tc) for tc in d.get("tool_calls", [])]
        return cls(
            role=MessageRole(d["role"]),
            content=d.get("content", ""),
            tool_call_id=d.get("tool_call_id"),
            tool_calls=tool_calls,
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


@dataclass(slots=True)
class LLMResponse:
    """Unified response from any LLM provider."""

    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


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
    ) -> LLMResponse:
        """Send messages and get a complete response."""
        ...

    @abstractmethod
    async def stream(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
    ) -> AsyncIterator[str]:
        """Stream text tokens as they arrive."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Clean up resources (HTTP clients, etc.)."""
        ...
