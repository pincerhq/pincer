"""A real Anthropic SDK client served scripted SSE responses (issue #345).

The SDK's own stream parser runs, so tests see what it really yields from
``text_stream`` and ``get_final_message()``, and every request body is recorded.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from anthropic import AsyncAnthropic

from pincer.llm.anthropic_common import AnthropicCompatibleProvider

if TYPE_CHECKING:
    from pincer.config import Settings


@dataclass
class Reject:
    """An HTTP error whose `error.message` is this text (Anthropic's own wording of a rejected beta, say)."""

    status: int
    message: str


# One scripted response: streamed blocks + stop reason (+ usage), an HTTP error status, or an
# exception the transport raises (e.g. httpx.ReadTimeout)
Scripted = (
    tuple[list[dict[str, Any]], str]
    | tuple[list[dict[str, Any]], str, dict[str, Any]]
    | tuple[list[dict[str, Any]], str, dict[str, Any], dict[str, Any]]
    | int
    | Reject
    | Exception
)


def thinking(text: str, signature: str) -> dict[str, Any]:
    return {"type": "thinking", "thinking": text, "signature": signature}


def redacted(data: str) -> dict[str, Any]:
    return {"type": "redacted_thinking", "data": data}


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


def tool_use(tool_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": arguments}


def compaction(summary: str | None, signature: str = "SIG-compaction") -> dict[str, Any]:
    """The block an on-demand compaction returns; it arrives whole, with no deltas (docs: compaction-on-demand)."""
    return {"type": "compaction", "content": summary, "signature": signature}


def _block_events(index: int, block: dict[str, Any]) -> list[dict[str, Any]]:
    kind = block["type"]
    if kind == "thinking":
        start = {"type": "thinking", "thinking": "", "signature": ""}
        deltas = [
            {"type": "thinking_delta", "thinking": block["thinking"]},
            {"type": "signature_delta", "signature": block["signature"]},
        ]
    elif kind in {"redacted_thinking", "compaction"}:
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
    blocks: list[dict[str, Any]],
    stop_reason: str,
    model: str = "claude-x",
    usage: dict[str, Any] | None = None,
    message: dict[str, Any] | None = None,
) -> bytes:
    """One streamed Messages response holding `blocks`, in order; `usage` adds to the start usage
    (e.g. ``cache_read_input_tokens``, ``iterations``), `message` adds fields to the message (e.g.
    ``input_transformations``)."""
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
                **(message or {}),
            },
        }
    ]
    for index, block in enumerate(blocks):
        events.extend(_block_events(index, block))
    events.append(
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": (usage or {}).get("output_tokens", 5)},
        }
    )
    events.append({"type": "message_stop"})
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


class ScriptedAnthropic:
    """An Anthropic provider whose HTTP responses are scripted, one per request:
    `(blocks, stop_reason)` or `(blocks, stop_reason, usage)` streams a response, an int
    returns that HTTP error, an exception is raised by the transport. ``provider`` is the provider
    name the leaf runs under ("anthropic", or a configured compatible endpoint's name)."""

    def __init__(self, settings: Settings, responses: list[Scripted], provider: str = "anthropic") -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.raw_requests: list[str] = []
        self.headers: list[httpx.Headers] = []  # each request's headers
        self.urls: list[httpx.URL] = []  # each request's URL
        # Request number (1-based) -> event: that request's response is held back until the event is set
        self.gates: dict[int, asyncio.Event] = {}
        self.provider = AnthropicCompatibleProvider(settings, provider)

    async def __aenter__(self) -> ScriptedAnthropic:
        await self.provider._client.close()
        self.provider._client = AsyncAnthropic(
            api_key="sk-ant-test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(self._serve))
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.provider.close()

    async def _serve(self, request: httpx.Request) -> httpx.Response:
        """`_handle`, then hold the response back while the request has a gate that is not set."""
        response = self._handle(request)
        gate = self.gates.get(len(self.requests))
        if gate is not None:
            await gate.wait()
        return response

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.raw_requests.append(request.content.decode())
        self.headers.append(request.headers)
        self.urls.append(request.url)
        self.requests.append(json.loads(request.content))
        if not self._responses:
            raise AssertionError(f"unscripted request #{len(self.requests)}")
        scripted = self._responses.pop(0)
        if isinstance(scripted, Exception):
            raise scripted
        if isinstance(scripted, Reject):
            error = {"type": "error", "error": {"type": "invalid_request_error", "message": scripted.message}}
            return httpx.Response(scripted.status, json=error)
        if isinstance(scripted, int):  # an HTTP error status (4xx is not retried by the SDK, except 429)
            error = {"type": "error", "error": {"type": "invalid_request_error", "message": "scripted failure"}}
            # A 429 asks for no wait: the SDK's own retries take 1 ms, the provider's retry 0 s
            headers = {"retry-after-ms": "1", "retry-after": "0"} if scripted == 429 else None
            return httpx.Response(scripted, json=error, headers=headers)
        blocks, stop_reason, *extras = scripted
        body = sse_body(
            blocks,
            stop_reason,
            usage=extras[0] if extras else None,
            message=extras[1] if len(extras) > 1 else None,
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)
