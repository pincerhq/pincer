"""The OpenAI Responses API's side of an OpenAI-wire provider (issue #358).

Pure conversions between Pincer's unified messages and Responses `input`/`output` items. The provider
(`openai_common.OpenAICompatibleProvider`) owns the requests; Chat Completions stays in that module.

A reply's output items are kept verbatim as `LLMResponse.raw` (reasoning items with their
`encrypted_content` included) and go back on later requests through the one replay rule in
`llm.replay`, so a reasoning model keeps its reasoning between tool calls and across turns.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pincer.llm.base import STOP_REASON_MAX_TOKENS, LLMMessage, LLMResponse, MessageRole, ToolCall
from pincer.llm.replay import replayable_raw

if TYPE_CHECKING:
    from openai.types.responses import Response

# What a request to any other wire (or model) sees of a compacted window: the window's compaction item is
# encrypted, so it cannot be rebuilt as text
WINDOW_NOTICE = "[Earlier conversation was compacted by OpenAI; its content is encrypted and cannot be read here]"


def convert_tools_to_responses(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic-style tool defs as Responses function tools: flat (not nested under `function`, as in
    Chat Completions), and explicitly not strict, since a tool left without `strict` is rewritten into
    strict mode where it can be."""
    return [
        {
            "type": "function",
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {}),
            "strict": False,
        }
        for tool in tools
    ]


def convert_messages_to_responses(messages: list[LLMMessage], wire: str) -> list[dict[str, Any]]:
    """Unified messages as Responses `input` items.

    An assistant message goes back as the provider's own output items (reasoning included) while it was
    produced by this provider and model (see `llm.replay`); any other is rebuilt from its text and tool
    calls. A compacted window (a SYSTEM message holding what `/responses/compact` returned) goes back the
    same way, and as `WINDOW_NOTICE` text on any other wire.
    """
    items: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == MessageRole.SYSTEM and (window := replayable_raw(msg, wire)) is not None:
            items.extend(window)  # a compacted window goes back as the API returned it, in its order
        elif msg.role == MessageRole.TOOL_RESULT:
            items.append({"type": "function_call_output", "call_id": msg.tool_call_id or "", "output": msg.content})
        elif msg.role == MessageRole.ASSISTANT:
            items.extend(_replayed_assistant(msg, wire) or _rebuilt_assistant(msg))
        elif msg.images or msg.context:
            items.append({"role": msg.role.value, "content": _user_parts(msg)})
        else:
            items.append({"role": msg.role.value, "content": msg.content})
    return items


def _user_parts(msg: LLMMessage) -> list[dict[str, Any]]:
    """Pincer's context block first, then the photos, then the user's words."""
    parts: list[dict[str, Any]] = []
    if msg.context:
        parts.append({"type": "input_text", "text": msg.context})
    parts.extend(
        {"type": "input_image", "image_url": f"data:{image.media_type};base64,{image.data}", "detail": "auto"}
        for image in msg.images
    )
    if msg.content:
        parts.append({"type": "input_text", "text": msg.content})
    return parts


def _replayed_assistant(msg: LLMMessage, wire: str) -> list[dict[str, Any]]:
    """The message's own output items when they go back as they were, else []: they are from another
    wire, or no longer match the message's tool calls (pair repair dropped one after the response), or
    only reasoning is left. A reasoning item after the last real item has nothing to belong to and is
    trimmed."""
    items = replayable_raw(msg, wire)
    if items is None or [i["call_id"] for i in items if i["type"] == "function_call"] != [
        tc.id for tc in msg.tool_calls
    ]:
        return []
    while items and items[-1]["type"] == "reasoning":
        items.pop()
    return items


def _rebuilt_assistant(msg: LLMMessage) -> list[dict[str, Any]]:
    """An assistant message as items built from its text and tool calls (no reasoning)."""
    items: list[dict[str, Any]] = []
    if msg.content or not msg.tool_calls:
        items.append({"role": "assistant", "content": msg.content})
    items.extend(
        {"type": "function_call", "call_id": tc.id, "name": tc.name, "arguments": json.dumps(tc.arguments)}
        for tc in msg.tool_calls
    )
    return items


def parse_responses_response(response: Response, wire: str) -> LLMResponse:
    """A Responses reply as a unified response: its output items verbatim (`raw`, with the wire that
    produced them), their text and its function calls. The usage is added by the caller."""
    text: list[str] = []
    tool_calls: list[ToolCall] = []
    for item in response.output:
        if item.type == "message":
            text.extend(part.text for part in item.content if part.type == "output_text")
        elif item.type == "function_call":
            try:
                arguments = json.loads(item.arguments)
            except json.JSONDecodeError:
                arguments = {}
            tool_calls.append(ToolCall(id=item.call_id, name=item.name, arguments=arguments))
    return LLMResponse(
        content="".join(text),
        tool_calls=tool_calls,
        model=response.model,
        stop_reason=_stop_reason(response, bool(tool_calls)),
        raw=[item.model_dump(mode="json", exclude_none=True) for item in response.output],
        wire=wire,
    )


def _stop_reason(response: Response, has_tool_calls: bool) -> str:
    """The reason Chat Completions would give: ``max_tokens`` when the output limit cut it off (like
    Anthropic, so one truncation check covers every wire), ``tool_calls`` or ``stop`` otherwise."""
    if response.status == "incomplete":
        reason = response.incomplete_details.reason if response.incomplete_details else None
        return STOP_REASON_MAX_TOKENS if reason == "max_output_tokens" else reason or "incomplete"
    return "tool_calls" if has_tool_calls else "stop"
