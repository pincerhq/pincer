"""A real OpenAI SDK client served scripted Chat Completions and Responses replies (issue #358, slice 6).

Both APIs are served by path from one transport, so a test sees which one an endpoint really posts to,
and every request is recorded (body, URL, headers). The SDK's own parser runs on the replies.

Also holds the scenario whose Chat Completions request body was captured from commit 6ac3e0e, before
the Responses API existed: an endpoint that is not on it must keep sending exactly those bytes.
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from openai import AsyncOpenAI
from pydantic import SecretStr

from pincer.llm.base import ImageContent, LLMMessage, MessageRole, ToolCall, prompt_cache_session
from pincer.llm.openai_common import OpenAICompatibleProvider

if TYPE_CHECKING:
    from pincer.config import Settings

FIXTURES = Path(__file__).resolve().parent / "fixtures"
# The cache key every scenario request carries (a fixed UUIDv7-shaped value)
GOLDEN_CACHE_KEY = "0190a1b2-c3d4-7e5f-8a6b-7c8d9e0f1a2b"
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


# ── Scripted replies ─────────────────────────────────────────────────────────


def reasoning_item(item_id: str = "rs_1", encrypted: str = "ENC-1") -> dict[str, Any]:
    return {"id": item_id, "type": "reasoning", "summary": [], "encrypted_content": encrypted}


def message_item(text: str, item_id: str = "msg_1") -> dict[str, Any]:
    content = {"type": "output_text", "text": text, "annotations": [], "logprobs": []}
    return {"id": item_id, "type": "message", "status": "completed", "role": "assistant", "content": [content]}


def function_call_item(
    name: str, arguments: dict[str, Any] | str, call_id: str = "call_1", item_id: str = "fc_1"
) -> dict[str, Any]:
    args = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {
        "id": item_id,
        "type": "function_call",
        "status": "completed",
        "call_id": call_id,
        "name": name,
        "arguments": args,
    }


def usage(
    input_tokens: int = 10, output_tokens: int = 5, cached: int = 0, reasoning: int = 0, written: int | None = None
) -> dict[str, Any]:
    details: dict[str, Any] = {"cached_tokens": cached}
    if written is not None:
        details["cache_write_tokens"] = written
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": details,
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": reasoning},
        "total_tokens": input_tokens + output_tokens,
    }


def response_body(
    output: list[dict[str, Any]],
    usage_: dict[str, Any] | None = None,
    model: str = "gpt-5.2",
    status: str = "completed",
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "resp_1",
        "object": "response",
        "created_at": 0,
        "model": model,
        "status": status,
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": usage_ if usage_ is not None else usage(),
    }
    if status == "incomplete":
        body["incomplete_details"] = {"reason": "max_output_tokens"}
    body.update(extras or {})
    return body


# A text reply that starts with this calls the named tool (with no arguments) instead of answering
CALL = "call:"


def completion_body(text: str, prompt_tokens: int = 10, model: str = "gpt-4o") -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": text}
    finish = "stop"
    if text.startswith(CALL):
        call = {"id": "call_1", "type": "function", "function": {"name": text[len(CALL) :], "arguments": "{}"}}
        message, finish = {"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls"
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 5, "total_tokens": prompt_tokens + 5},
    }


def _chat_stream(reply: str, prompt_tokens: int, model: str) -> httpx.Response:
    """A streamed Chat Completions reply: the text (or a tool call) in one chunk, then the usage."""
    delta: dict[str, Any] = {"role": "assistant", "content": reply}
    finish = "stop"
    if reply.startswith(CALL):
        call = {"index": 0, "id": "call_1", "type": "function"}
        call["function"] = {"name": reply[len(CALL) :], "arguments": "{}"}  # type: ignore[assignment]
        delta, finish = {"role": "assistant", "tool_calls": [call]}, "tool_calls"
    chunk = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    usage_ = {**chunk, "choices": [], "usage": completion_body(reply, prompt_tokens)["usage"]}
    sse = f"data: {json.dumps(chunk)}\n\ndata: {json.dumps(usage_)}\n\ndata: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode())


# ── Streamed Responses replies ───────────────────────────────────────────────

# The encrypted_content an `output_item.added` event carries while the item is still in progress: the
# docs say it "may be incomplete", only the `output_item.done` item is complete
PARTIAL_ENCRYPTED = "PARTIAL"


def _sse(events: list[dict[str, Any]]) -> bytes:
    """Server-sent events: each dict is one event named by its `type`."""
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def responses_events(
    output: list[dict[str, Any]],
    usage_: dict[str, Any] | None = None,
    model: str = "gpt-5.2",
    status: str = "completed",
    text_chunks: list[str] | None = None,
    terminal_output: bool = True,
) -> list[dict[str, Any]]:
    """The events of a streamed Responses reply for ``output`` items, as the API orders them: created,
    then per item `added`, its deltas and `done`, then the terminal event holding the whole response.

    ``text_chunks`` splits the (first) message's text into deltas; an unsplit text is one delta. A
    reasoning item is `added` with a PARTIAL encrypted_content. ``terminal_output`` False sends the
    terminal response with no output items (the items then only exist in the `done` events).
    """
    terminal = {"completed": "response.completed", "incomplete": "response.incomplete", "failed": "response.failed"}
    seq = iter(range(1000))
    whole = response_body(output, usage_, model, status)
    events: list[dict[str, Any]] = [
        {
            "type": "response.created",
            "sequence_number": next(seq),
            "response": {**whole, "output": [], "status": "in_progress"},
        }
    ]
    for index, item in enumerate(output):
        at = {"sequence_number": next(seq), "output_index": index}
        if item["type"] == "reasoning":
            added = {**item, "encrypted_content": PARTIAL_ENCRYPTED}
        elif item["type"] == "message":
            added = {**item, "status": "in_progress", "content": []}
        elif item["type"] == "function_call":
            added = {**item, "status": "in_progress", "arguments": ""}
        else:
            added = item
        events.append({"type": "response.output_item.added", **at, "item": added})
        if item["type"] == "message":
            part = item["content"][0]
            chunks = text_chunks if text_chunks is not None else [part["text"]]
            ids = {"item_id": item["id"], "output_index": index, "content_index": 0}
            empty = {**part, "text": ""}
            events.append({"type": "response.content_part.added", "sequence_number": next(seq), **ids, "part": empty})
            for chunk in chunks:
                events.append(
                    {
                        "type": "response.output_text.delta",
                        "sequence_number": next(seq),
                        **ids,
                        "delta": chunk,
                        "logprobs": [],
                    }
                )
            done = {"type": "response.output_text.done", "sequence_number": next(seq), **ids, "text": part["text"]}
            events.append({**done, "logprobs": []})
            events.append({"type": "response.content_part.done", "sequence_number": next(seq), **ids, "part": part})
        elif item["type"] == "function_call":
            args = item["arguments"]
            half = len(args) // 2
            for piece in (args[:half], args[half:]):
                events.append(
                    {
                        "type": "response.function_call_arguments.delta",
                        "sequence_number": next(seq),
                        "item_id": item["id"],
                        "output_index": index,
                        "delta": piece,
                    }
                )
            events.append(
                {
                    "type": "response.function_call_arguments.done",
                    "sequence_number": next(seq),
                    "item_id": item["id"],
                    "output_index": index,
                    "name": item["name"],
                    "arguments": args,
                }
            )
        events.append({"type": "response.output_item.done", **at, "item": item})
    final = whole if terminal_output else {**whole, "output": []}
    events.append({"type": terminal[status], "sequence_number": next(seq), "response": final})
    return events


class RecordingStream(httpx.AsyncByteStream):
    """A streamed HTTP body that remembers whether the client closed it; with ``hang`` it never ends
    after its chunks (a model still generating)."""

    def __init__(self, chunks: list[bytes], hang: bool = False) -> None:
        self._chunks = chunks
        self._hang = hang
        self.closed = False

    async def __aiter__(self) -> Any:
        for chunk in self._chunks:
            yield chunk
        if self._hang:
            await asyncio.sleep(3600)

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class Call:
    """One tool call of a scripted turn; its arguments arrive in several chunks."""

    name: str
    arguments: list[str]
    call_id: str = "call_1"


@dataclass
class Turn:
    """One scripted streamed reply, served as Chat Completions chunks to a Chat request and as Responses
    events to a Responses request, so both APIs can be given exactly the same reply."""

    text: list[str] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    reasoning: bool = False
    prompt_tokens: int = 100
    completion_tokens: int = 12
    cached: int = 0
    truncated: bool = False

    def usage(self) -> dict[str, Any]:
        return usage(self.prompt_tokens, self.completion_tokens, self.cached)

    def items(self) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = [reasoning_item()] if self.reasoning else []
        if self.text:
            output.append(message_item("".join(self.text)))
        for n, call in enumerate(self.calls):
            output.append(function_call_item(call.name, "".join(call.arguments), call.call_id, f"fc_{n}"))
        return output


def _chat_turn_chunks(turn: Turn, model: str) -> httpx.Response:
    """A streamed Chat Completions reply of ``turn``: a chunk per text piece, a first chunk per tool
    call then its argument pieces, the finish reason, then the usage."""

    def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
        choice = {"index": 0, "delta": delta, "finish_reason": finish}
        return {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
            "choices": [choice],
        }

    chunks = [chunk({"role": "assistant", "content": piece}) for piece in turn.text]
    for index, call in enumerate(turn.calls):
        head = {
            "index": index,
            "id": call.call_id,
            "type": "function",
            "function": {"name": call.name, "arguments": ""},
        }
        chunks.append(chunk({"tool_calls": [head]}))
        for piece in call.arguments:
            chunks.append(chunk({"tool_calls": [{"index": index, "function": {"arguments": piece}}]}))
    finish = "length" if turn.truncated else "tool_calls" if turn.calls else "stop"
    chunks.append(chunk({}, finish))
    usage_ = {"prompt_tokens": turn.prompt_tokens, "completion_tokens": turn.completion_tokens}
    usage_["total_tokens"] = turn.prompt_tokens + turn.completion_tokens
    if turn.cached:
        usage_["prompt_tokens_details"] = {"cached_tokens": turn.cached}  # type: ignore[assignment]
    chunks.append({**chunk({}), "choices": [], "usage": usage_})
    sse = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode())


class Incomplete(list[dict[str, Any]]):
    """Output items of a response that stopped on `max_output_tokens`."""


@dataclass
class RawStream:
    """A Responses stream of exactly these events (an error shape, an unknown event, a cut-off stream);
    with ``hang`` the body never ends after them."""

    events: list[dict[str, Any]]
    hang: bool = False


@dataclass
class Compaction:
    """A reply of `/responses/compact`: the compacted window and the usage of the compaction pass. With
    a ``gate`` the reply is held until the test sets it, so a turn can run while the compaction is in flight."""

    items: list[dict[str, Any]]
    usage: dict[str, Any] | None = None
    gate: asyncio.Event | None = None


@dataclass
class Status:
    """An HTTP error reply with a body of its own; with a ``gate`` it is held until the test sets it, so
    several requests can be in flight when the first answer arrives."""

    code: int
    body: dict[str, Any] | None = None
    gate: asyncio.Event | None = None


def compaction_item(item_id: str = "cmp_1", encrypted: str = "ENC-CMP-1", **extra: Any) -> dict[str, Any]:
    return {"id": item_id, "type": "compaction", "encrypted_content": encrypted, **extra}


def user_item(text: str, item_id: str = "msg_u1") -> dict[str, Any]:
    """A user message as the compact endpoint retains it."""
    return {
        "id": item_id,
        "type": "message",
        "status": "completed",
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
    }


def compacted_body(items: list[dict[str, Any]], usage_: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": "resp_cmp",
        "object": "response.compaction",
        "created_at": 0,
        "output": items,
        "usage": usage_ if usage_ is not None else usage(),
    }


# What a scripted reply can be: output items (a Responses reply), a Chat text, an HTTP error status,
# a scripted streamed turn, raw stream events, or a compaction
Reply = (
    list[dict[str, Any]]
    | str
    | int
    | Exception
    | tuple[list[dict[str, Any]], dict[str, Any]]
    | Turn
    | RawStream
    | Compaction
    | Status
)


class ScriptedOpenAI:
    """An OpenAI provider on a MockTransport: Responses replies from output items, Chat replies from text.

    `requests` holds every body, `paths` the URL path and `urls` the full URL of each, in order.
    """

    def __init__(
        self,
        settings: Settings,
        replies: list[Reply],
        provider: str = "openai",
        model: str = "gpt-5.2",
        base_url: str | None = None,
        prompt_tokens: int = 10,
        extras: dict[str, Any] | None = None,
    ) -> None:
        self._extras = extras  # top-level fields added to every Responses reply
        self._replies = list(replies)
        self._model = model
        self._prompt_tokens = prompt_tokens
        self.requests: list[dict[str, Any]] = []
        self.paths: list[str] = []
        self.urls: list[str] = []
        self.headers: list[httpx.Headers] = []
        self.streams: list[RecordingStream] = []  # the HTTP body of each streamed reply served
        self.provider = OpenAICompatibleProvider(settings, provider)
        url = base_url if base_url is not None else str(self.provider._client.base_url)
        # No SDK retries: a test sees the provider's own handling of a 429
        self.provider._client = AsyncOpenAI(
            api_key=self.provider._client.api_key,
            base_url=url,
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self._serve)),
        )

    async def _serve(self, request: httpx.Request) -> httpx.Response:
        """`_handle`, then a held `Compaction` reply waits for its gate."""
        pending = self._replies[0] if self._replies else None
        response = self._handle(request)
        if isinstance(pending, Compaction | Status) and pending.gate is not None:
            await pending.gate.wait()
        return response

    def _stream(self, content: bytes, hang: bool = False) -> httpx.Response:
        body = RecordingStream([content], hang)
        self.streams.append(body)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)

    def _items_events(self, reply: Reply) -> list[dict[str, Any]]:
        """The events of a streamed reply scripted as output items (or a Chat text)."""
        if isinstance(reply, str):
            reply = [function_call_item(reply[len(CALL) :], {}) if reply.startswith(CALL) else message_item(reply)]
        if isinstance(reply, tuple):
            output, usage_ = reply
            return responses_events(output, usage_, self._model)
        assert isinstance(reply, list)
        status = "incomplete" if isinstance(reply, Incomplete) else "completed"
        return responses_events(reply, usage(self._prompt_tokens), self._model, status)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        self.paths.append(request.url.path)
        self.urls.append(str(request.url))
        self.headers.append(request.headers)
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, int):
            return httpx.Response(reply, json={"error": {"message": f"scripted {reply}", "type": "scripted"}})
        if isinstance(reply, RawStream):
            return self._stream(_sse(reply.events), reply.hang)
        if isinstance(reply, Status):
            error = reply.body or {"error": {"message": f"scripted {reply.code}", "type": "scripted"}}
            return httpx.Response(reply.code, json=error)
        if isinstance(reply, Compaction):
            assert request.url.path.endswith("/responses/compact"), f"a compaction served to {request.url.path}"
            return httpx.Response(200, json=compacted_body(reply.items, reply.usage))
        if isinstance(reply, Turn):
            if not request.url.path.endswith("/responses"):
                return _chat_turn_chunks(reply, self._model)
            status = "incomplete" if reply.truncated else "completed"
            events = responses_events(reply.items(), reply.usage(), self._model, status, text_chunks=reply.text)
            return self._stream(_sse(events))
        if request.url.path.endswith("/responses"):
            if body.get("stream"):
                return self._stream(_sse(self._items_events(reply)))
            if isinstance(reply, str):  # a Chat text served to a Responses request: still a valid reply
                reply = [function_call_item(reply[len(CALL) :], {}) if reply.startswith(CALL) else message_item(reply)]
            if isinstance(reply, tuple):
                output, usage_ = reply
                return httpx.Response(200, json=response_body(output, usage_, self._model, extras=self._extras))
            status = "incomplete" if isinstance(reply, Incomplete) else "completed"
            return httpx.Response(
                200, json=response_body(reply, usage(self._prompt_tokens), self._model, status, self._extras)
            )
        if not isinstance(
            reply, str
        ):  # Responses output served to a Chat request: its text, so the test fails on its own assertion
            items = reply[0] if isinstance(reply, tuple) else reply
            reply = "".join(p["text"] for i in items if i["type"] == "message" for p in i["content"])
        if body.get("stream"):
            return _chat_stream(reply, self._prompt_tokens, self._model)
        return httpx.Response(200, json=completion_body(reply, self._prompt_tokens, self._model))


# ── The golden Chat Completions request ──────────────────────────────────────

GOLDEN_TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_weather",
        "description": "Weather for a city",
        "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    },
    {"name": "note", "input_schema": {"type": "object", "properties": {}}},
]


def golden_messages() -> list[LLMMessage]:
    """A history with every message shape the Chat converter knows: summary, context + photo + words,
    an assistant tool call, its result, an assistant reply, a plain user turn."""
    photo = ImageContent.from_bytes(PNG_1X1, "image/png")
    return [
        LLMMessage(role=MessageRole.SYSTEM, content="[Previous conversation summary]\nThe user likes tea."),
        LLMMessage(
            role=MessageRole.USER,
            content="What is in this photo and the weather?",
            images=[photo],
            context="It is 09:00.",
        ),
        LLMMessage(
            role=MessageRole.ASSISTANT,
            content="Checking.",
            tool_calls=[ToolCall(id="call_1", name="get_weather", arguments={"city": "Kyiv"})],
        ),
        LLMMessage(role=MessageRole.TOOL_RESULT, content="Sunny, 21C", tool_call_id="call_1"),
        LLMMessage(role=MessageRole.ASSISTANT, content="A cat, and it is sunny."),
        LLMMessage(role=MessageRole.USER, content="Thanks"),
    ]


GOLDEN_SCENARIOS: dict[str, dict[str, Any]] = {
    "plain": {"model": "llama-3.3-70b", "reasoning_effort": ""},
    "reasoning": {"model": "gpt-5.2", "reasoning_effort": "high"},
}


async def golden_chat_request(
    settings: Settings, scenario: str, provider: str = "groq", stream: bool = False
) -> tuple[str, str]:
    """(URL path, body text) an OpenAI-wire endpoint named ``provider`` sends for the golden history,
    for a streamed turn when ``stream``."""
    spec = GOLDEN_SCENARIOS[scenario]
    pinned = settings.model_copy(
        update={
            "openai_compatible_provider": provider,
            "openai_compatible_base_url": "http://127.0.0.1:8000/v1",
            "openai_compatible_api_key": SecretStr("sk-test"),
            "openai_compatible_model": spec["model"],
            "prompt_cache_key_providers": ["openai", provider],
            "reasoning_effort": spec["reasoning_effort"],
            "thinking": "",
            "temperature": 0.5,
            "max_tokens": 4096,
        }
    )
    seen: list[tuple[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.content.decode()))
        if request.url.path.endswith(
            "/responses"
        ):  # the wrong API still gets a valid reply: the test fails on its path
            if stream:
                events = responses_events([message_item("ok")], model=spec["model"])
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_sse(events))
            return httpx.Response(200, json=response_body([message_item("ok")], model=spec["model"]))
        if stream:
            return _chat_stream("ok", 10, spec["model"])
        return httpx.Response(200, json=completion_body("ok", model=spec["model"]))

    llm = OpenAICompatibleProvider(pinned, provider)
    llm._client = AsyncOpenAI(
        api_key="sk-test",
        base_url="http://127.0.0.1:8000/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    with prompt_cache_session(lambda: GOLDEN_CACHE_KEY):
        if stream:
            [e async for e in llm.stream_turn(golden_messages(), tools=GOLDEN_TOOLS, system="You are Pincer.")]
        else:
            await llm.complete(golden_messages(), tools=GOLDEN_TOOLS, system="You are Pincer.")
    assert len(seen) == 1
    return seen[0]
