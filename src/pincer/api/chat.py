"""Web chat API — endpoints for the browser chat widget.

All routes are under `/api/`, so they are gated by the existing
`auth_middleware` in `pincer.api.server`. We do not add a second auth
check here.

The streaming endpoint multiplexes two producers onto a single SSE
response: agent events (text chunks, tool start/done, final done) and
approval prompts emitted from inside `_execute_tool` via the
`set_send_event` contextvar. Both go through an asyncio.Queue so the
agent can pause on `await self._approval_callback(...)` while the SSE
stream stays open and the browser receives the `approval` event in
real time.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from pincer.api.approvals import (
    request_web_approval,
    reset_send_event,
    resolve_web_approval,
    set_send_event,
)
from pincer.core.agent import StreamEventType
from pincer.exceptions import BudgetExceededError, LLMError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pincer.core.agent import Agent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_CHANNEL = "web"
_DONE_SENTINEL = object()


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)


class ApprovalIn(BaseModel):
    approval_id: str = Field(min_length=1, max_length=128)
    approved: bool


def _require_user(value: str | None) -> str:
    if not value or not _UUID_RE.match(value):
        raise HTTPException(400, "Missing or invalid X-Pincer-User header")
    return value


def _get_agent(request: Request) -> Agent:
    agent = getattr(request.app.state, "agent", None)
    if agent is None:
        raise HTTPException(503, "Agent is not initialized")
    return agent


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.post("/message")
async def post_message(
    body: ChatIn,
    request: Request,
    x_pincer_user: str | None = Header(default=None, alias="X-Pincer-User"),
) -> dict:
    user_id = _require_user(x_pincer_user)
    agent = _get_agent(request)
    try:
        resp = await agent.handle_message(user_id, _CHANNEL, body.text)
    except BudgetExceededError as e:
        raise HTTPException(402, str(e)) from e
    except LLMError as e:
        raise HTTPException(502, str(e)) from e
    return {"reply": resp.text, "cost_usd": resp.cost_usd, "model": resp.model}


@router.post("/stream")
async def post_stream(
    body: ChatIn,
    request: Request,
    x_pincer_user: str | None = Header(default=None, alias="X-Pincer-User"),
) -> StreamingResponse:
    user_id = _require_user(x_pincer_user)
    agent = _get_agent(request)

    async def gen() -> AsyncIterator[str]:
        # Multiplex agent stream events and approval-prompt events onto one queue.
        queue: asyncio.Queue[tuple[str, dict] | object] = asyncio.Queue()

        async def send_event(name: str, data: dict[str, Any]) -> None:
            await queue.put((name, data))

        token = set_send_event(send_event)

        async def driver() -> None:
            try:
                async for chunk in agent.handle_message_stream(user_id, _CHANNEL, body.text):
                    if chunk.type == StreamEventType.TEXT:
                        await queue.put(("chunk", {"delta": chunk.content}))
                    elif chunk.type == StreamEventType.TOOL_START:
                        await queue.put(("tool", {"phase": "start", "name": chunk.content}))
                    elif chunk.type == StreamEventType.TOOL_DONE:
                        await queue.put(("tool", {"phase": "done", "name": chunk.content}))
                    elif chunk.type == StreamEventType.DONE:
                        await queue.put(("done", {"text": chunk.content}))
            except BudgetExceededError as e:
                await queue.put(("error", {"message": f"Warning: Daily budget — {e}"}))
            except LLMError as e:
                await queue.put(("error", {"message": f"LLM error: {e}"}))
            except Exception as e:  # noqa: BLE001
                logger.exception("Chat stream failed")
                await queue.put(("error", {"message": str(e)}))
            finally:
                await queue.put(_DONE_SENTINEL)

        task = asyncio.create_task(driver())
        try:
            while True:
                item = await queue.get()
                if item is _DONE_SENTINEL:
                    break
                name, data = item  # type: ignore[misc]
                yield _sse(name, data)
        finally:
            reset_send_event(token)
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/approval")
async def post_approval(
    body: ApprovalIn,
    x_pincer_user: str | None = Header(default=None, alias="X-Pincer-User"),
) -> dict:
    user_id = _require_user(x_pincer_user)
    if not resolve_web_approval(body.approval_id, user_id, body.approved):
        raise HTTPException(404, "Unknown, expired, or unauthorized approval")
    return {"ok": True}


# Re-exported for tests.
__all__ = ["router", "request_web_approval", "resolve_web_approval"]
