"""A real Anthropic SDK client served scripted SSE responses (issue #345).

The SDK's own stream parser runs, so tests see what it really yields from
``text_stream`` and ``get_final_message()``, and every request body is recorded.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
from anthropic import AsyncAnthropic

from pincer.llm.anthropic_common import AnthropicCompatibleProvider

if TYPE_CHECKING:
    from pincer.config import Settings

# One scripted response: streamed blocks + stop reason (+ usage), or an HTTP error status
Scripted = tuple[list[dict[str, Any]], str] | tuple[list[dict[str, Any]], str, dict[str, int]] | int


def thinking(text: str, signature: str) -> dict[str, Any]:
    return {"type": "thinking", "thinking": text, "signature": signature}


def redacted(data: str) -> dict[str, Any]:
    return {"type": "redacted_thinking", "data": data}


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


def tool_use(tool_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": arguments}


def _block_events(index: int, block: dict[str, Any]) -> list[dict[str, Any]]:
    kind = block["type"]
    if kind == "thinking":
        start = {"type": "thinking", "thinking": "", "signature": ""}
        deltas = [
            {"type": "thinking_delta", "thinking": block["thinking"]},
            {"type": "signature_delta", "signature": block["signature"]},
        ]
    elif kind == "redacted_thinking":
        start, deltas = dict(block), []
    elif kind == "text":
        start, deltas = {"type": "text", "text": ""}, [{"type": "text_delta", "text": block["text"]}]
    else:
        start = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
        deltas = [{"type": "input_json_delta", "partial_json": json.dumps(block["input"])}]
    return [
        {"type": "content_block_start", "index": index, "content_block": start},
        *({"type": "content_block_delta", "index": index, "delta": d} for d in deltas),
        {"type": "content_block_stop", "index": index},
    ]


def sse_body(
    blocks: list[dict[str, Any]], stop_reason: str, model: str = "claude-x", usage: dict[str, int] | None = None
) -> bytes:
    """One streamed Messages response holding `blocks`, in order; `usage` adds to the start usage
    (e.g. ``cache_read_input_tokens``)."""
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 0, **(usage or {})},
            },
        }
    ]
    for index, block in enumerate(blocks):
        events.extend(_block_events(index, block))
    events.append(
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": 5},
        }
    )
    events.append({"type": "message_stop"})
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


class ScriptedAnthropic:
    """An Anthropic provider whose HTTP responses are scripted, one per request:
    `(blocks, stop_reason)` or `(blocks, stop_reason, usage)` streams a response, an int
    returns that HTTP error."""

    def __init__(self, settings: Settings, responses: list[Scripted]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.raw_requests: list[str] = []
        self.provider = AnthropicCompatibleProvider(settings, "anthropic")

    async def __aenter__(self) -> ScriptedAnthropic:
        await self.provider._client.close()
        self.provider._client = AsyncAnthropic(
            api_key="sk-ant-test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(self._handle))
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.provider.close()

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.raw_requests.append(request.content.decode())
        self.requests.append(json.loads(request.content))
        if not self._responses:
            raise AssertionError(f"unscripted request #{len(self.requests)}")
        scripted = self._responses.pop(0)
        if isinstance(scripted, int):  # an HTTP error status (4xx is not retried by the SDK)
            error = {"type": "error", "error": {"type": "invalid_request_error", "message": "scripted failure"}}
            return httpx.Response(scripted, json=error)
        blocks, stop_reason, *usage = scripted
        body = sse_body(blocks, stop_reason, usage=usage[0] if usage else None)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)
