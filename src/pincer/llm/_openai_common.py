"""
Shared helpers for OpenAI-compatible providers (OpenAI, Grok, Deep Seek).

Converts LLMMessage to OpenAI API format and parses ChatCompletion responses.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pincer.llm.base import LLMMessage, LLMResponse, MessageRole, ToolCall

if TYPE_CHECKING:
    from openai.types.chat import ChatCompletion


def convert_tools_to_openai(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Anthropic-style tool defs to OpenAI function-calling format."""
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
        elif msg.images:
            content_parts: list[dict[str, Any]] = []
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


def parse_openai_response(response: ChatCompletion) -> LLMResponse:
    """Parse OpenAI ChatCompletion into unified LLMResponse."""
    choice = response.choices[0]
    message = choice.message
    tool_calls: list[ToolCall] = []

    if message.tool_calls:
        for tc in message.tool_calls:
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))

    return LLMResponse(
        content=message.content or "",
        tool_calls=tool_calls,
        model=response.model,
        input_tokens=response.usage.prompt_tokens if response.usage else 0,
        output_tokens=response.usage.completion_tokens if response.usage else 0,
        stop_reason=choice.finish_reason or "",
    )
