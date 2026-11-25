"""Web-channel approval flow.

Telegram and WhatsApp resolve approvals via a per-user `Future` owned by
the channel. The web channel has no persistent socket back to the
server, so it drives approvals over SSE + a small REST endpoint:

  1. Agent reaches a `require_approval=True` tool and calls
     `request_web_approval(...)`.
  2. We mint a UUID, push an `approval` event onto the SSE stream so the
     browser can render an "Approve / Deny" card, and `await` an
     `asyncio.Future` keyed by that UUID.
  3. The user clicks. Frontend POSTs `/api/chat/approval` with the
     approval_id + decision. `resolve_web_approval` sets the future, the
     agent unblocks, the SSE stream resumes.
  4. Once decided, or expired after the timeout, we push an
     `approval_resolved` event (`status`: approved / denied / expired) so
     the browser can show the card's final state.

The per-request `send_event` callable is propagated through a
`contextvars.ContextVar` so the agent's approval callback (set once at
agent construction) can find the right SSE stream for the current
request.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

from pincer.core.approval import ApprovalOutcome, await_decision

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# Per-request callable: (event_name, data) -> Awaitable[None]
SendEvent = "Callable[[str, dict[str, Any]], Awaitable[None]]"

_send_event_var: contextvars.ContextVar[Callable[[str, dict[str, Any]], Awaitable[None]] | None] = (
    contextvars.ContextVar("pincer_send_event", default=None)
)

# approval_id -> (user_id, future)
_pending: dict[str, tuple[str, asyncio.Future[bool]]] = {}

_DEFAULT_TIMEOUT = 120.0
_MAX_STR_LEN = 500

# Final card state sent in the `approval_resolved` event. UNAVAILABLE has no
# entry: it means no card was shown, and those paths return before the event.
_RESOLVED_STATUS = {
    ApprovalOutcome.APPROVED: "approved",
    ApprovalOutcome.DENIED: "denied",
    ApprovalOutcome.TIMED_OUT: "expired",
}


def set_send_event(
    send_event: Callable[[str, dict[str, Any]], Awaitable[None]],
) -> contextvars.Token[Callable[[str, dict[str, Any]], Awaitable[None]] | None]:
    """Bind a per-request `send_event` callable to the current context."""
    return _send_event_var.set(send_event)


def reset_send_event(
    token: contextvars.Token[Callable[[str, dict[str, Any]], Awaitable[None]] | None],
) -> None:
    _send_event_var.reset(token)


def _sanitize_args(args: dict[str, Any]) -> dict[str, Any]:
    """Make tool arguments safe to JSON-serialize for the SSE payload."""
    out: dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, (bytes, bytearray)):
            out[k] = f"<bytes len={len(v)}>"
            continue
        if isinstance(v, str) and len(v) > _MAX_STR_LEN:
            out[k] = v[:_MAX_STR_LEN] + "… [truncated]"
            continue
        try:
            json.dumps(v)
            out[k] = v
        except (TypeError, ValueError):
            out[k] = str(v)
    return out


async def request_web_approval(
    tool_name: str,
    arguments: dict[str, Any],
    user_id: str,
    channel: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> ApprovalOutcome:
    """Approval callback for the web channel.

    Signature matches `pincer.core.agent.ApprovalCallback`. Returns
    UNAVAILABLE when the prompt can't be shown: a non-web channel (the web
    agent should never see one, but the guard makes the wiring fail-safe),
    no SSE stream, or a failed emit. TIMED_OUT when nobody answers.
    """
    if channel != "web":
        logger.warning("request_web_approval invoked for non-web channel %s; approval unavailable", channel)
        return ApprovalOutcome.UNAVAILABLE

    send_event = _send_event_var.get()
    if send_event is None:
        logger.warning(
            "Web approval requested for %s but no SSE stream is bound to this request; approval unavailable",
            tool_name,
        )
        return ApprovalOutcome.UNAVAILABLE

    approval_id = uuid.uuid4().hex
    fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    _pending[approval_id] = (user_id, fut)

    try:
        await send_event(
            "approval",
            {
                "approval_id": approval_id,
                "tool": tool_name,
                "args": _sanitize_args(arguments),
            },
        )
    except Exception:
        logger.exception("Failed to emit approval SSE event for %s", tool_name)
        _pending.pop(approval_id, None)
        return ApprovalOutcome.UNAVAILABLE

    try:
        outcome = await await_decision(fut, timeout)
    finally:
        _pending.pop(approval_id, None)
    if outcome is ApprovalOutcome.TIMED_OUT:
        logger.info("Web approval %s timed out for tool %s", approval_id, tool_name)

    try:
        await send_event("approval_resolved", {"approval_id": approval_id, "status": _RESOLVED_STATUS[outcome]})
    except Exception:
        logger.debug("Failed to emit approval_resolved for %s", approval_id, exc_info=True)
    return outcome


def resolve_web_approval(approval_id: str, user_id: str, approved: bool) -> bool:
    """Resolve a pending approval. Returns True if the future was set.

    Returns False (caller should 404) if:
      - approval_id is unknown / already resolved / expired
      - the resolving user_id does not match the user who started it
    """
    entry = _pending.get(approval_id)
    if entry is None:
        return False
    expected_user, fut = entry
    if expected_user != user_id or fut.done():
        return False
    fut.set_result(approved)
    return True


def pending_count() -> int:
    """Test/debug helper — number of currently-pending approvals."""
    return len(_pending)
