"""A real OpenAI SDK client served scripted Chat Completions and Responses replies (issue #358, slice 6).

Both APIs are served by path from one transport, so a test sees which one an endpoint really posts to,
and every request is recorded (body, URL, headers). The SDK's own parser runs on the replies.

Also holds the scenario whose Chat Completions request body was captured from commit 6ac3e0e, before
the Responses API existed: an endpoint that is not on it must keep sending exactly those bytes.
"""

from __future__ import annotations

import base64
import json
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


class Incomplete(list[dict[str, Any]]):
    """Output items of a response that stopped on `max_output_tokens`."""


# What a scripted reply can be: output items (a Responses reply), a Chat text, or an HTTP error status
Reply = list[dict[str, Any]] | str | int | Exception | tuple[list[dict[str, Any]], dict[str, Any]]


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
        self.provider = OpenAICompatibleProvider(settings, provider)
        url = base_url if base_url is not None else str(self.provider._client.base_url)
        # No SDK retries: a test sees the provider's own handling of a 429
        self.provider._client = AsyncOpenAI(
            api_key=self.provider._client.api_key,
            base_url=url,
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self._handle)),
        )

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
        if request.url.path.endswith("/responses"):
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


async def golden_chat_request(settings: Settings, scenario: str, provider: str = "groq") -> tuple[str, str]:
    """(URL path, body text) an OpenAI-wire endpoint named ``provider`` sends for the golden history."""
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
        ):  # the wrong API still gets a valid reply: the test then fails on its path
            return httpx.Response(200, json=response_body([message_item("ok")], model=spec["model"]))
        return httpx.Response(200, json=completion_body("ok", model=spec["model"]))

    llm = OpenAICompatibleProvider(pinned, provider)
    llm._client = AsyncOpenAI(
        api_key="sk-test",
        base_url="http://127.0.0.1:8000/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    with prompt_cache_session(lambda: GOLDEN_CACHE_KEY):
        await llm.complete(golden_messages(), tools=GOLDEN_TOOLS, system="You are Pincer.")
    assert len(seen) == 1
    return seen[0]
