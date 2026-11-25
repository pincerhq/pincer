"""Web chat API — endpoints for the browser chat widget.

All routes are under `/api/`, so they are gated by the existing
`auth_middleware` in `pincer.api.server`. We do not add a second auth
check here — only decide whose conversation this is (`chat_user`).

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
import base64
import binascii
import contextlib
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from pincer.api.approvals import (
    request_web_approval,
    reset_send_event,
    resolve_web_approval,
    set_send_event,
)
from pincer.api.auth import OptionalIdentity
from pincer.core.agent import StreamEventType
from pincer.core.ratelimit import Rate, RateLimitResult, rate_limit
from pincer.core.session import channel_session_key, conversation_key
from pincer.exceptions import BudgetCheckFailedError, LLMError
from pincer.services.sessions import SessionServiceDep
from pincer.services.voice import CallsServiceDep

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pincer.core.agent import Agent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])


def _message_rate() -> Rate:
    """The channels' per-user message rate (`PINCER_RATE_MESSAGES_PER_MIN`), read per request."""
    from pincer.config import get_settings_relaxed

    return Rate.per_minute(get_settings_relaxed().rate_messages_per_min)


def _too_fast(result: RateLimitResult) -> str:
    """The channels' wording, so web chat refuses the same way."""
    from pincer.security.rate_limiter import too_fast_message

    return too_fast_message(result.retry_after)


# Each turn is an LLM call: web chat gets the channels' per-user message limit
# (a token bucket, as there). `name="chat"` gives /message and /stream one budget.
_CHAT_LIMIT = rate_limit(_message_rate, name="chat", strategy="token_bucket", detail=_too_fast)

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_CHANNEL = "web"
_DONE_SENTINEL = object()


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    # One web chat of the caller's ("New chat", a call summary), as its own
    # model context. Scoped under the chat user, so whatever value is sent it
    # never reaches anyone else's conversation.
    conversation_id: str | None = Field(default=None, pattern=_UUID_RE.pattern)
    # False for a request that is not the person talking to Pincer (a call
    # summary, a knowledge-base question): their long-term memory is neither
    # searched for it nor written with it, and onboarding leaves it alone.
    # It needs its own conversation: the conversation stays memory-free from
    # then on, which the user's main chat must never become.
    memory: bool = True

    @field_validator("conversation_id")
    @classmethod
    def _canonical_uuid(cls, value: str | None) -> str | None:
        """One spelling per id, so a change of case is not a second session."""
        return None if value is None else str(uuid.UUID(value))

    @model_validator(mode="after")
    def _memory_free_needs_conversation(self) -> ChatIn:
        if not self.memory and self.conversation_id is None:
            raise ValueError("memory=false requires a conversation_id")
        return self


class CallReportOut(BaseModel):
    """The final report of a phone call asked for in this conversation."""

    call_sid: str
    outcome: str
    text: str
    created_at: str
    reported_at: str


class ConversationOut(BaseModel):
    """One web chat conversation of the caller's."""

    id: str = Field(description="The conversation id, as sent in `conversation_id`")
    title: str = Field(description="The first message of the conversation, clipped")
    last_message_at: datetime
    message_count: int = Field(description="The messages its transcript shows")


class ConversationsOut(BaseModel):
    """A page of the caller's web chat conversations, latest activity first."""

    items: list[ConversationOut]
    next_cursor: str | None = Field(description="Pass as `cursor` for the next page; null on the last page")


class TranscriptMessageOut(BaseModel):
    """A message as the person saw it: what they wrote, or Pincer's text."""

    id: str
    role: str = Field(description="`user` or `assistant`")
    content: str
    interrupted: bool = Field(
        description="A reply cut short: the client went away while it streamed, or the daily budget stopped it"
    )
    images: int = Field(description="The photos the message carried (a photo-only message has empty `content`)")
    created_at: datetime


class TranscriptOut(BaseModel):
    """A page of a conversation's transcript, oldest first."""

    messages: list[TranscriptMessageOut] = Field(description="Oldest first")
    has_more: bool = Field(description="Older messages exist")
    next_before: str | None = Field(description="Pass as `before` for the older page; null when there is none")


class ApprovalIn(BaseModel):
    approval_id: str = Field(min_length=1, max_length=128)
    approved: bool


def _require_user(value: str | None) -> str:
    if not value or not _UUID_RE.match(value):
        raise HTTPException(400, "Missing or invalid X-Pincer-User header")
    return value


def chat_user(
    identity: OptionalIdentity,
    x_pincer_user: str | None = Header(default=None, alias="X-Pincer-User"),
) -> str:
    """Whose conversation this request belongs to.

    - A signed-in session is that identity, whatever the header says: a
      person must not be able to read or continue someone else's thread by
      naming them.
    - An API key is typically one key embedded in a widget that many
      visitors use. `X-Pincer-User` is then the visitor's session id, so
      they do not all share one conversation; without it the key's own
      identity is used.
    - With PINCER_AUTH_DISABLED there is no identity and the header is
      required.
    """
    if identity is None:
        return _require_user(x_pincer_user)
    if identity.interactive or x_pincer_user is None:
        return identity.pincer_user_id
    return _require_user(x_pincer_user)


ChatUser = Annotated[str, Depends(chat_user)]


def _conversation_session_key(user_id: str, conversation_id: str | None) -> str | None:
    """The conversation's session, or None for the user's main web session."""
    return conversation_key(user_id, _CHANNEL, conversation_id) if conversation_id else None


#: The characters of a conversation's first message its title keeps.
_TITLE_CHARS = 80


async def _require_own_conversation(
    sessions: SessionServiceDep, user_id: str, conversation_id: str, *, claimed: bool
) -> str:
    """The caller's session key for `conversation_id`, or a 404.

    Args:
        sessions: The session store.
        user_id: The caller.
        conversation_id: The conversation id, canonical.
        claimed: Whether the caller must own the id already (reading it). Without, an id nobody has
            used yet is free too (posting to it): the first turn sent with it makes it the caller's.

    Returns:
        The caller's session key for the conversation.

    Raises:
        HTTPException: 404 when another identity owns the id, or with `claimed` when nobody does.
    """
    key = conversation_key(user_id, _CHANNEL, conversation_id)
    owner = await sessions.conversation_owner(conversation_id)
    if owner != key and (claimed or owner is not None):
        raise HTTPException(404, "Unknown conversation")
    return key


def _encode_cursor(at: datetime, row_id: str) -> str:
    return base64.urlsafe_b64encode(f"{at.isoformat()}|{row_id}".encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        text = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        at, row_id = text.split("|", 1)
        return datetime.fromisoformat(at), str(uuid.UUID(row_id))
    except (binascii.Error, UnicodeDecodeError, ValueError) as e:
        raise HTTPException(422, "Invalid cursor") from e


def _clipped(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= _TITLE_CHARS else line[: _TITLE_CHARS - 1].rstrip() + "…"


def _get_agent(request: Request) -> Agent:
    agent: Agent | None = getattr(request.app.state, "agent", None)
    if agent is None:
        raise HTTPException(503, "Agent is not initialized")
    return agent


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.post("/message")
@_CHAT_LIMIT
async def post_message(
    body: ChatIn,
    request: Request,
    user_id: ChatUser,
    sessions: SessionServiceDep,
) -> dict[str, Any]:
    agent = _get_agent(request)
    if body.conversation_id is not None:
        await _require_own_conversation(sessions, user_id, body.conversation_id, claimed=False)
    try:
        resp = await agent.handle_message(
            user_id,
            _CHANNEL,
            body.text,
            session_key=_conversation_session_key(user_id, body.conversation_id),
            memory=body.memory,
        )
    except BudgetCheckFailedError as e:
        # The same 503 as a turn that loses the budget mid-way: nothing was sent or saved
        raise HTTPException(503, str(e)) from e
    except LLMError as e:
        raise HTTPException(502, str(e)) from e
    if (resp.budget_reached or resp.budget_check_failed) and not resp.call_sids:
        # A refusal, not a reply: 402 when the budget is reached, 503 when it could not be
        # read mid-turn. A turn that placed a call still answers 200, so the client keeps
        # waiting for that call's report.
        raise HTTPException(503 if resp.budget_check_failed else 402, resp.text)
    if resp.budget_reached or resp.budget_check_failed:
        # As the stream's `done`: the reply is the model's own text, the warning beside it. A
        # turn that wrote nothing (it only dialled) replies with the warning: never an empty bubble.
        return {
            "reply": resp.turn_text or resp.text,
            "budget_warning": resp.text,
            "cost_usd": resp.cost_usd,
            "model": resp.model,
            "call_sids": resp.call_sids,
        }
    return {"reply": resp.text, "cost_usd": resp.cost_usd, "model": resp.model, "call_sids": resp.call_sids}


@router.post("/stream")
@_CHAT_LIMIT
async def post_stream(
    body: ChatIn,
    request: Request,
    user_id: ChatUser,
    sessions: SessionServiceDep,
) -> StreamingResponse:
    agent = _get_agent(request)
    # Before the stream opens, so a refused id is a plain 404, not an `error` event
    if body.conversation_id is not None:
        await _require_own_conversation(sessions, user_id, body.conversation_id, claimed=False)

    async def gen() -> AsyncIterator[str]:
        # Multiplex agent stream events and approval-prompt events onto one queue.
        queue: asyncio.Queue[tuple[str, dict[str, Any]] | object] = asyncio.Queue()

        async def send_event(name: str, data: dict[str, Any]) -> None:
            await queue.put((name, data))

        token = set_send_event(send_event)

        async def driver() -> None:
            placed_call = False
            try:
                async with contextlib.aclosing(
                    agent.handle_message_stream(
                        user_id,
                        _CHANNEL,
                        body.text,
                        session_key=_conversation_session_key(user_id, body.conversation_id),
                        memory=body.memory,
                    )
                ) as stream:
                    async for chunk in stream:
                        if chunk.type == StreamEventType.TEXT:
                            await queue.put(("chunk", {"delta": chunk.content}))
                        elif chunk.type == StreamEventType.TOOL_START:
                            await queue.put(("tool", {"phase": "start", "name": chunk.content}))
                        elif chunk.type == StreamEventType.TOOL_DONE:
                            done: dict[str, Any] = {"phase": "done", "name": chunk.content}
                            # A placed call: its report comes later from /call-reports.
                            if chunk.call_sid:
                                done["call_sid"] = chunk.call_sid
                                placed_call = True
                            await queue.put(("tool", done))
                        elif chunk.type == StreamEventType.DONE:
                            if (chunk.budget_reached or chunk.budget_check_failed) and not placed_call:
                                # A refusal, not a reply: clients key on the error event. A turn
                                # that placed a call ends with `done`, as `POST /message` answers
                                # 200, so the client keeps waiting for that call's report.
                                await queue.put(("error", {"message": chunk.content, "cost_usd": chunk.cost_usd}))
                            elif chunk.budget_reached or chunk.budget_check_failed:
                                # `text` stays what was streamed, so a client that shows it keeps
                                # the turn's words; the warning comes beside it
                                stopped = {"text": chunk.turn_text or chunk.content, "cost_usd": chunk.cost_usd}
                                await queue.put(("done", {**stopped, "budget_warning": chunk.content}))
                            else:
                                await queue.put(("done", {"text": chunk.content, "cost_usd": chunk.cost_usd}))
            except BudgetCheckFailedError as e:
                # Refused before any request, in the shape of a budget refusal
                await queue.put(("error", {"message": str(e), "cost_usd": 0.0}))
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
    user_id: ChatUser,
) -> dict[str, Any]:
    if not resolve_web_approval(body.approval_id, user_id, body.approved):
        raise HTTPException(404, "Unknown, expired, or unauthorized approval")
    return {"ok": True}


@router.get("/call-reports", response_model=list[CallReportOut])
async def get_call_reports(
    user_id: ChatUser,
    calls: CallsServiceDep,
    conversation_id: Annotated[str | None, Query(pattern=_UUID_RE.pattern)] = None,
    since: Annotated[datetime | None, Query(description="Only reports delivered at or after this time")] = None,
) -> list[CallReportOut]:
    """Final reports of the calls placed from this conversation (or the main
    web chat), oldest first. Web chat has no push path, so the browser polls
    this after a call is placed and once when a conversation is opened."""
    conversation = None if conversation_id is None else str(uuid.UUID(conversation_id))
    session_key = _conversation_session_key(user_id, conversation) or channel_session_key(user_id, _CHANNEL)
    # Stored timestamps are UTC ISO text, compared as such on SQLite.
    cutoff = None
    if since is not None:
        cutoff = (since.astimezone(UTC) if since.tzinfo else since.replace(tzinfo=UTC)).isoformat()
    rows = await calls.list_call_reports(user_id, session_key, cutoff)
    return [
        CallReportOut(
            call_sid=str(row["call_sid"]),
            outcome=str(row.get("report_outcome") or ""),
            text=str(row.get("report_text") or ""),
            created_at=str(row.get("created_at") or ""),
            reported_at=str(row.get("reported_at") or ""),
        )
        for row in rows
    ]


@router.get("/conversations", response_model=ConversationsOut)
async def get_conversations(
    user_id: ChatUser,
    sessions: SessionServiceDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(description="`next_cursor` of the previous page")] = None,
) -> ConversationsOut:
    """The caller's web chat conversations, latest activity first.

    Call summaries and knowledge-base questions (`memory: false`) are not listed.

    Args:
        user_id: The caller.
        sessions: The session store.
        limit: The conversations per page.
        cursor: `next_cursor` of the previous page; the first page without.

    Returns:
        The page, with the cursor of the next one.

    Raises:
        HTTPException: 422 for a cursor this endpoint did not hand out.
    """
    before = None if cursor is None else _decode_cursor(cursor)
    rows = await sessions.conversations(user_id, _CHANNEL, limit=limit + 1, before=before)
    page = rows[:limit]
    next_cursor = _encode_cursor(page[-1]["updated_at"], page[-1]["id"]) if len(rows) > limit else None
    return ConversationsOut(
        items=[
            ConversationOut(
                id=row["conversation_id"],
                title=_clipped(row["title"]),
                last_message_at=row["updated_at"],
                message_count=row["message_count"],
            )
            for row in page
        ],
        next_cursor=next_cursor,
    )


@router.get("/conversations/{conversation_id}/messages", response_model=TranscriptOut)
async def get_conversation_messages(
    conversation_id: Annotated[str, Path(pattern=_UUID_RE.pattern)],
    user_id: ChatUser,
    sessions: SessionServiceDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[str | None, Query(description="`next_before` of the newer page")] = None,
) -> TranscriptOut:
    """A conversation's transcript as the person saw it, oldest first.

    Everything compaction took out of the model's context is still here.

    Args:
        conversation_id: The conversation, as sent in `conversation_id`.
        user_id: The caller.
        sessions: The session store.
        limit: The messages per page: the newest ones, or those before `before`.
        before: `next_before` of the newer page; the newest page without.

    Returns:
        The page, and whether older messages exist.

    Raises:
        HTTPException: 404 unless the caller owns the conversation; 422 for a cursor this endpoint did
            not hand out.
    """
    key = await _require_own_conversation(sessions, user_id, str(uuid.UUID(conversation_id)), claimed=True)
    cursor = None if before is None else _decode_cursor(before)
    rows = await sessions.transcript(key, limit=limit + 1, before=cursor)
    has_more = len(rows) > limit
    page = rows[1:] if has_more else rows
    return TranscriptOut(
        messages=[
            TranscriptMessageOut(
                id=str(row["id"]),
                role=row["role"],
                content=row["content"],
                interrupted=bool(row["is_error"]) and row["role"] == "assistant",
                images=len(row["images"] or ()),
                created_at=row["created_at"],
            )
            for row in page
        ],
        has_more=has_more,
        next_before=_encode_cursor(page[0]["created_at"], str(page[0]["id"])) if has_more and page else None,
    )


# Re-exported for tests.
__all__ = ["router", "request_web_approval", "resolve_web_approval"]
