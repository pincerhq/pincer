"""
Live call status updates to the initiating user (Sprint 1, T1.5).

During an outbound call the user who asked for it gets short status messages
on their originating channel via the ChannelRouter:
    📞 Dialing…  ->  📞 Connected  ->  📞 Call ended — <outcome>

Hard cap: at most one message per stage, three stages per call — never spam.
The notifier callback is wired in cli.py (router.send_to_user); when no
notifier is set (tests, inbound-only deployments) everything is a no-op.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

STAGE_DIALING = "dialing"
STAGE_CONNECTED = "connected"
STAGE_ENDED = "ended"


@dataclass
class _OutboundCallInfo:
    user_id: str
    channel: str = ""
    purpose: str = ""
    target_number: str = ""
    language: str = ""
    target_name: str = ""
    instructions: str = ""
    notified_stages: set[str] = field(default_factory=set)


_notifier: Callable[[str, str, str], Awaitable[bool]] | None = None
_outbound_calls: dict[str, _OutboundCallInfo] = {}
_MAX_TRACKED = 200


def set_status_notifier(notifier: Callable[[str, str, str], Awaitable[bool]] | None) -> None:
    """Install the delivery callback: async (user_id, channel, text) -> bool."""
    global _notifier  # noqa: PLW0603
    _notifier = notifier


def register_outbound_call(
    call_sid: str,
    user_id: str,
    channel: str = "",
    purpose: str = "",
    target_number: str = "",
    language: str = "",
    target_name: str = "",
    instructions: str = "",
) -> None:
    """Track an outbound call so status updates can reach its initiating user
    (name and instructions ride along into the call prompt)."""
    if not call_sid or not user_id:
        return
    _outbound_calls[call_sid] = _OutboundCallInfo(
        user_id=user_id,
        channel=channel,
        purpose=purpose,
        target_number=target_number,
        language=language,
        target_name=target_name,
        instructions=instructions,
    )
    while len(_outbound_calls) > _MAX_TRACKED:
        _outbound_calls.pop(next(iter(_outbound_calls)), None)


def get_call_user(call_sid: str) -> str:
    info = _outbound_calls.get(call_sid)
    return info.user_id if info else ""


def get_call_language(call_sid: str) -> str:
    """Language of a tracked outbound call ('' if unknown)."""
    info = _outbound_calls.get(call_sid)
    return info.language if info else ""


def get_call_info(call_sid: str) -> _OutboundCallInfo | None:
    return _outbound_calls.get(call_sid)


def clear_call(call_sid: str) -> None:
    """Stop tracking a call (after its final message has been delivered)."""
    _outbound_calls.pop(call_sid, None)


async def send_user_message(user_id: str, channel: str, text: str) -> bool:
    """Deliver a one-off message through the installed notifier, outside the
    per-call stage tracking (used by the appointment retry policy, whose
    updates span several call SIDs)."""
    if _notifier is None or not user_id:
        return False
    try:
        return await _notifier(user_id, channel, text)
    except Exception:
        logger.exception("Direct user notify failed [%s]", user_id)
        return False


async def notify_stage(call_sid: str, stage: str, text: str) -> bool:
    """Send one status message for a stage; repeat calls for a stage are dropped."""
    info = _outbound_calls.get(call_sid)
    if not info or _notifier is None:
        return False
    if stage in info.notified_stages:
        return False
    info.notified_stages.add(stage)
    try:
        return await _notifier(info.user_id, info.channel, text)
    except Exception:
        logger.exception("Status notify failed [%s, %s]", call_sid, stage)
        return False


async def notify_dialing(call_sid: str) -> bool:
    info = _outbound_calls.get(call_sid)
    detail = f" {info.target_number}" if info and info.target_number else ""
    return await notify_stage(call_sid, STAGE_DIALING, f"📞 Dialing{detail}…")


async def notify_connected(call_sid: str) -> bool:
    return await notify_stage(call_sid, STAGE_CONNECTED, "📞 Connected")


async def notify_ended(call_sid: str, outcome: str = "") -> bool:
    """Final status — always sent exactly once, including on failure paths."""
    text = f"📞 Call ended — {outcome}" if outcome else "📞 Call ended — preparing summary"
    sent = await notify_stage(call_sid, STAGE_ENDED, text)
    _outbound_calls.pop(call_sid, None)
    return sent


def _reset_for_tests() -> None:
    global _notifier  # noqa: PLW0603
    _notifier = None
    _outbound_calls.clear()
