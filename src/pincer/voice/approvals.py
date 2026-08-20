"""
Voice-call approval broker (Sprint 11, §6.3/§6.5) — ``user`` mode.

While the call partner is on hold, the initiating Pincer user is asked on
their own channel whether a Tier W action may run. This module owns the
pending requests and their futures; channels only *present* the card and
*resolve* the answer:

  presenter(request)  -> bool        show the card on the user's channel
  finalizer(request, final_state)    edit the card: approved/denied/expired/call-ended
  resolve(approval_id, approved)     called by the channel's button handler
  cancel_for_call(call_sid)          the callee hung up while waiting

Nothing here speaks — the in-call gate does the hold/reassure lines and
decides what happens on each outcome. The wait is bounded server-side
(``PINCER_VOICE_APPROVAL_TIMEOUT_S``); the card is always edited to a final
state so a stale "approve?" never lingers on the user's screen.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

APPROVED = "approved"
DENIED = "denied"
EXPIRED = "expired"
CALL_ENDED = "call_ended"
FINAL_STATES = (APPROVED, DENIED, EXPIRED, CALL_ENDED)

_MAX_STR_LEN = 300


@dataclass
class VoiceApprovalRequest:
    approval_id: str
    call_sid: str
    tool_name: str
    summary: str
    summary_spoken_language: str
    args_preview: dict[str, Any]
    expires_at: str
    user_id: str
    channel: str
    future: asyncio.Future[bool] = field(repr=False)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    final_state: str = ""
    # Presenter bookkeeping (e.g. Telegram chat/message ids for the card edit)
    extra: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        """The §6.5 payload — the same dict for Telegram, dashboard, and tests."""
        return {
            "type": "voice_call_action",
            "approval_id": self.approval_id,
            "call_sid": self.call_sid,
            "tool_name": self.tool_name,
            "summary_spoken_language": self.summary_spoken_language,
            "summary": self.summary,
            "args_preview": dict(self.args_preview),
            "expires_at": self.expires_at,
            "created_at": self.created_at,
            "final_state": self.final_state,
        }

    @property
    def is_pending(self) -> bool:
        return not self.final_state and not self.future.done()


Presenter = "Callable[[VoiceApprovalRequest], Awaitable[bool]]"
Finalizer = "Callable[[VoiceApprovalRequest, str], Awaitable[None]]"

_presenter: Callable[[VoiceApprovalRequest], Awaitable[bool]] | None = None
_finalizer: Callable[[VoiceApprovalRequest, str], Awaitable[None]] | None = None
_pending: dict[str, VoiceApprovalRequest] = {}
_MAX_TRACKED = 200


def set_presenter(presenter: Callable[[VoiceApprovalRequest], Awaitable[bool]] | None) -> None:
    global _presenter  # noqa: PLW0603
    _presenter = presenter


def set_finalizer(finalizer: Callable[[VoiceApprovalRequest, str], Awaitable[None]] | None) -> None:
    global _finalizer  # noqa: PLW0603
    _finalizer = finalizer


def has_presenter() -> bool:
    return _presenter is not None


def _sanitize_args(args: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (args or {}).items():
        if isinstance(value, bytes | bytearray):
            out[key] = f"<bytes len={len(value)}>"
        elif isinstance(value, str) and len(value) > _MAX_STR_LEN:
            out[key] = value[:_MAX_STR_LEN] + "…"
        elif isinstance(value, str | int | float | bool) or value is None:
            out[key] = value
        else:
            out[key] = str(value)[:_MAX_STR_LEN]
    return out


async def request(
    *,
    call_sid: str,
    tool_name: str,
    summary: str,
    language: str,
    args_preview: dict[str, Any] | None,
    user_id: str,
    channel: str,
    timeout_s: float,
) -> VoiceApprovalRequest:
    """Create a pending request and present it. ``request.extra['presented']``
    tells the caller whether anyone can actually answer it."""
    loop = asyncio.get_running_loop()
    req = VoiceApprovalRequest(
        approval_id=uuid.uuid4().hex,
        call_sid=call_sid,
        tool_name=tool_name,
        summary=summary,
        summary_spoken_language=language,
        args_preview=_sanitize_args(args_preview),
        expires_at=(datetime.now(UTC) + timedelta(seconds=timeout_s)).isoformat(),
        user_id=user_id,
        channel=channel,
        future=loop.create_future(),
    )
    _pending[req.approval_id] = req
    while len(_pending) > _MAX_TRACKED:
        oldest = next(iter(_pending))
        _pending.pop(oldest, None)

    presented = False
    if _presenter is not None and user_id:
        try:
            presented = bool(await _presenter(req))
        except Exception:
            logger.exception("Voice approval presenter failed [%s]", call_sid)
    req.extra["presented"] = presented
    if not presented:
        logger.warning(
            "Voice approval for %s on call %s could not be presented (channel=%s, user=%s)",
            tool_name,
            call_sid,
            channel,
            user_id or "-",
        )
    return req


def resolve(approval_id: str, approved: bool, *, by_user_id: str | None = None) -> bool:
    """Button handler entry point. Returns True when the answer was accepted."""
    req = _pending.get(approval_id)
    if req is None or not req.is_pending:
        return False
    if by_user_id is not None and req.user_id and str(by_user_id) != str(req.user_id):
        logger.warning("Voice approval %s answered by %s, expected %s — ignored", approval_id, by_user_id, req.user_id)
        return False
    req.final_state = APPROVED if approved else DENIED
    req.future.set_result(bool(approved))
    return True


async def finalize(req: VoiceApprovalRequest, final_state: str) -> None:
    """Mark the request final (idempotent) and edit the card."""
    if final_state not in FINAL_STATES:
        final_state = EXPIRED
    if not req.final_state:
        req.final_state = final_state
    if not req.future.done():
        req.future.set_result(False)
    _pending.pop(req.approval_id, None)
    if _finalizer is not None:
        try:
            await _finalizer(req, req.final_state)
        except Exception:
            logger.exception("Voice approval finalizer failed [%s]", req.approval_id)


async def cancel_for_call(call_sid: str) -> int:
    """The callee hung up: every pending card for the call becomes 'call ended'
    and nothing executes. Returns how many were cancelled."""
    count = 0
    for req in [r for r in _pending.values() if r.call_sid == call_sid and r.is_pending]:
        await finalize(req, CALL_ENDED)
        count += 1
    return count


def get(approval_id: str) -> VoiceApprovalRequest | None:
    return _pending.get(approval_id)


def pending(call_sid: str | None = None) -> list[VoiceApprovalRequest]:
    return [r for r in _pending.values() if r.is_pending and (call_sid is None or r.call_sid == call_sid)]


def _reset_for_tests() -> None:
    global _presenter, _finalizer  # noqa: PLW0603
    _presenter = None
    _finalizer = None
    for req in list(_pending.values()):
        if not req.future.done():
            req.future.cancel()
    _pending.clear()


__all__ = [
    "APPROVED",
    "CALL_ENDED",
    "DENIED",
    "EXPIRED",
    "FINAL_STATES",
    "VoiceApprovalRequest",
    "cancel_for_call",
    "finalize",
    "get",
    "has_presenter",
    "pending",
    "request",
    "resolve",
    "set_finalizer",
    "set_presenter",
]
