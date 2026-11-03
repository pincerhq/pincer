"""
Live call status updates to the initiating user (Sprint 1, T1.5).

During an outbound call the user who asked for it gets short status messages
on their originating channel via the ChannelRouter:
    📞 Dialing…  ->  📞 Connected  ->  📞 Call ended — <outcome>

Hard cap: at most one message per stage, three stages per call — never spam.
The notifier callback is wired in cli.py (router.send_to_user); when no
notifier is set (tests, inbound-only deployments) everything is a no-op.

Issue #303: every final report goes through `finalize_report`, which also
writes it into the chat session that asked for the call (the session
reporter) and stores it on the call's `pincer_call_origins` row. That row is
written at dial time, so the routing survives a restart: `hydrate()` reloads
the calls still waiting for their report, and `reconcile()` finalizes the
ones that ended while the process was down.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

STAGE_DIALING = "dialing"
STAGE_CONNECTED = "connected"
STAGE_ENDED = "ended"

# Short outcome codes stored with the final report (`report_outcome`).
OUTCOME_COMPLETED = "completed"
OUTCOME_NO_ANSWER = "no-answer"
OUTCOME_BUSY = "busy"
OUTCOME_VOICEMAIL = "voicemail"
OUTCOME_FAILED = "failed"
OUTCOME_CANCELED = "canceled"
OUTCOME_INTERRUPTED = "interrupted"

# How long a call may ring before it is answered or given up: added to
# `voice_max_call_duration` for the window `hydrate()` reloads.
RINGING_ALLOWANCE_S = 600
# Bound on the whole startup reconcile, so a slow Twilio never holds it open.
RECONCILE_TIMEOUT_S = 60.0
_MAX_TRACKED = 200


@dataclass
class _OutboundCallInfo:
    user_id: str
    channel: str = ""
    purpose: str = ""
    target_number: str = ""
    language: str = ""
    target_name: str = ""
    instructions: str = ""
    pincer_user_id: str = ""
    # The chat session that asked for the call; "" for calls with no chat
    # (dashboard, scheduled, demo, canary), which skip the session report.
    session_key: str = ""
    # Reloaded from its origin row after a restart: any live state it had is gone.
    restored: bool = False
    notified_stages: set[str] = field(default_factory=set)


_notifier: Callable[[str, str, str], Awaitable[bool]] | None = None
_session_reporter: Callable[[str, str, str, str], Awaitable[None]] | None = None
_db_path: str = ""
_outbound_calls: dict[str, _OutboundCallInfo] = {}
# Calls whose final report is being delivered: a second ending (the /status
# webhook racing the post-call pipeline) must not deliver it again meanwhile.
_finalizing: set[str] = set()


def set_status_notifier(notifier: Callable[[str, str, str], Awaitable[bool]] | None) -> None:
    """Install the delivery callback: async (user_id, channel, text) -> bool."""
    global _notifier  # noqa: PLW0603
    _notifier = notifier


def set_session_reporter(reporter: Callable[[str, str, str, str], Awaitable[None]] | None) -> None:
    """Install the chat-session callback: async (pincer_user_id, channel,
    session_key, text) -> None. It adds the final report to the session the
    call was asked for in, so the agent knows how the call went."""
    global _session_reporter  # noqa: PLW0603
    _session_reporter = reporter


def set_report_store(db_path: str | Path | None) -> None:
    """The database the call origins and reports are kept in ("" = memory only)."""
    global _db_path  # noqa: PLW0603
    _db_path = str(db_path or "")


def register_outbound_call(
    call_sid: str,
    user_id: str,
    channel: str = "",
    purpose: str = "",
    target_number: str = "",
    language: str = "",
    target_name: str = "",
    instructions: str = "",
    pincer_user_id: str = "",
    session_key: str = "",
) -> None:
    """Track an outbound call so status updates can reach its initiating user
    (name and instructions ride along into the call prompt). In memory only;
    `track_outbound_call` also persists it."""
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
        pincer_user_id=pincer_user_id,
        session_key=session_key,
    )
    while len(_outbound_calls) > _MAX_TRACKED:
        _outbound_calls.pop(next(iter(_outbound_calls)), None)


async def track_outbound_call(call_sid: str, user_id: str, **fields: str) -> None:
    """`register_outbound_call`, then write the call's origin row.

    The in-memory record stays the read path; the row is what a restart
    rebuilds it from. Best effort: a database error is logged and the call
    goes on with in-memory tracking only.
    """
    register_outbound_call(call_sid, user_id, **fields)
    info = _outbound_calls.get(call_sid)
    service = await _store()
    if info is None or service is None:
        return
    try:
        await service.save_call_origin(
            {
                "call_sid": call_sid,
                "user_id": info.user_id,
                "pincer_user_id": info.pincer_user_id,
                "channel": info.channel,
                "session_key": info.session_key,
                "language": info.language,
                "target_number": info.target_number,
                "target_name": info.target_name,
                "purpose": info.purpose,
                "instructions": info.instructions,
                "created_at": datetime.now(UTC).isoformat(),
            }
        )
    except Exception:
        logger.warning("Call origin not saved [%s] — its report will not survive a restart", call_sid, exc_info=True)


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


async def notify_ended(call_sid: str, outcome: str = "", *, outcome_code: str = "") -> bool:
    """Final status — always sent exactly once, including on failure paths."""
    text = f"📞 Call ended — {outcome}" if outcome else "📞 Call ended — preparing summary"
    return await finalize_report(call_sid, text, outcome_code)


def _not_connected_code(provider_status: str, failure_code: str) -> str:
    """Why nobody was reached, when Twilio's status or the failure code says ("" otherwise)."""
    if provider_status in (OUTCOME_BUSY, OUTCOME_NO_ANSWER, OUTCOME_FAILED, OUTCOME_CANCELED):
        return provider_status
    by_code = {"busy": OUTCOME_BUSY, "no_answer": OUTCOME_NO_ANSWER, "voicemail": OUTCOME_VOICEMAIL}
    return by_code.get(failure_code, "")


def outcome_code_for(provider_status: str = "", failure_code: str = "") -> str:
    """The short `report_outcome` code for a call that reached nobody, from
    Twilio's status when it has one, else from the failure code."""
    code = _not_connected_code(provider_status, failure_code)
    if code:
        return code
    return OUTCOME_COMPLETED if failure_code in ("", "none") else OUTCOME_FAILED


def final_outcome_code(completed: bool, end_code: str = "", provider_status: str = "") -> str:
    """The `report_outcome` code of a call that had state, the same whichever
    path reports it (the post-call pipeline or a fallback): the reason nobody
    was reached when Twilio's status or the end code names one, else whether
    the call completed."""
    code = _not_connected_code(provider_status, end_code)
    if code:
        return code
    return OUTCOME_COMPLETED if completed else OUTCOME_FAILED


async def finalize_report(call_sid: str, text: str, outcome: str = "") -> bool:
    """Deliver a call's final report, once, everywhere it belongs.

    1. Stores it on the call's origin row. A row that already holds a report
       (a duplicate after a restart) stops here: nothing is sent twice.
    2. Pushes it to the initiating user's channel (the ENDED stage).
    3. Adds it to the chat session that asked for the call, when known.

    Returns whether the channel push was delivered. The call is no longer
    tracked afterwards.
    """
    if call_sid in _finalizing:
        return False
    _finalizing.add(call_sid)
    try:
        info = _outbound_calls.get(call_sid) or await _load_info(call_sid)
        if info is None or STAGE_ENDED in info.notified_stages:
            return False
        if not await _claim_report(call_sid, info, outcome, text):
            logger.info("Report for %s was already delivered — not sending it again", call_sid)
            return False
        delivered = await notify_stage(call_sid, STAGE_ENDED, text)
        if info.session_key and _session_reporter is not None:
            try:
                target = info.target_name or info.target_number or call_sid
                note = f"📞 Call report ({target}):\n{text}"
                await _session_reporter(info.pincer_user_id or info.user_id, info.channel, info.session_key, note)
            except Exception:
                logger.exception("Call report not added to its chat session [%s]", call_sid)
        return delivered
    finally:
        _outbound_calls.pop(call_sid, None)
        _finalizing.discard(call_sid)


async def _store() -> Any:
    """The calls service on the report store, or None (none configured, or unusable)."""
    if not _db_path:
        return None
    try:
        from pincer.services.voice import CallsService

        return await CallsService.for_path(Path(_db_path))
    except Exception:
        logger.warning("Call report store unavailable", exc_info=True)
        return None


async def _claim_report(call_sid: str, info: _OutboundCallInfo, outcome: str, text: str) -> bool:
    """Write the report to the origin row; False only when the row already
    holds one. A missing row is written whole, and a database error lets the
    report go out anyway: losing it is worse than a rare duplicate."""
    service = await _store()
    if service is None:
        return True
    now = datetime.now(UTC).isoformat()
    try:
        if await service.complete_call_report(call_sid, outcome, text, now):
            return True
        if await service.get_call_origin(call_sid) is not None:
            return False
        await service.save_call_origin(
            {
                "call_sid": call_sid,
                "user_id": info.user_id,
                "pincer_user_id": info.pincer_user_id,
                "channel": info.channel,
                "session_key": info.session_key,
                "language": info.language,
                "target_number": info.target_number,
                "target_name": info.target_name,
                "purpose": info.purpose,
                "instructions": info.instructions,
                "created_at": now,
                "report_outcome": outcome,
                "report_text": text,
                "reported_at": now,
            }
        )
    except Exception:
        logger.warning("Call report not stored [%s]", call_sid, exc_info=True)
    return True


def _info_from_row(row: dict[str, Any]) -> _OutboundCallInfo:
    return _OutboundCallInfo(
        user_id=str(row.get("user_id") or ""),
        channel=str(row.get("channel") or ""),
        purpose=str(row.get("purpose") or ""),
        target_number=str(row.get("target_number") or ""),
        language=str(row.get("language") or ""),
        target_name=str(row.get("target_name") or ""),
        instructions=str(row.get("instructions") or ""),
        pincer_user_id=str(row.get("pincer_user_id") or ""),
        session_key=str(row.get("session_key") or ""),
        restored=True,
    )


async def _load_info(call_sid: str) -> _OutboundCallInfo | None:
    """The routing of an untracked call from its unreported origin row (one
    whose report is due before `hydrate()` got to it, or evicted from memory)."""
    service = await _store()
    if service is None or not call_sid:
        return None
    try:
        row = await service.get_call_origin(call_sid)
    except Exception:
        logger.debug("Call origin lookup failed [%s]", call_sid, exc_info=True)
        return None
    if row is None or row.get("reported_at") or not row.get("user_id"):
        return None
    info = _info_from_row(row)
    _outbound_calls.setdefault(call_sid, info)
    return _outbound_calls[call_sid]


async def ensure_tracked(call_sid: str) -> _OutboundCallInfo | None:
    """The call's routing, from memory or else its unreported origin row."""
    return _outbound_calls.get(call_sid) or await _load_info(call_sid)


async def hydrate(max_call_duration_s: int) -> list[str]:
    """Reload the calls still waiting for their final report after a restart.

    Only calls placed within `max_call_duration_s` plus the ringing allowance:
    anything older has ended, and its report is lost with the process that
    had its state (see `reconcile`). Returns the SIDs reloaded.
    """
    service = await _store()
    if service is None:
        return []
    cutoff = datetime.now(UTC) - timedelta(seconds=max_call_duration_s + RINGING_ALLOWANCE_S)
    try:
        rows = await service.list_unreported_since(cutoff.isoformat())
    except Exception:
        logger.warning("Call origins not reloaded — reports of calls placed before the restart are lost", exc_info=True)
        return []
    loaded: list[str] = []
    for row in rows:
        call_sid = str(row.get("call_sid") or "")
        if not call_sid or not row.get("user_id") or call_sid in _outbound_calls:
            continue
        _outbound_calls[call_sid] = _info_from_row(row)
        loaded.append(call_sid)
    if loaded:
        logger.info("Reloaded %d outbound call(s) awaiting their report", len(loaded))
    return loaded


def interrupted_report(call_sid: str) -> str:
    """The report of a call that was live when the process restarted."""
    from pincer.voice.prompts import get_prompt

    info = _outbound_calls.get(call_sid)
    language = info.language if info else ""
    target = (info.target_name or info.target_number) if info else ""
    return str(get_prompt("CALL_INTERRUPTED_REPORT", language)).format(target=target or "unknown")


async def finalize_interrupted(call_sid: str) -> bool:
    """Finalize a call whose live state was lost to a restart."""
    return await finalize_report(call_sid, interrupted_report(call_sid), OUTCOME_INTERRUPTED)


async def finalize_not_connected(call_sid: str, status: str) -> bool:
    """Finalize a call Twilio reports as never connected (busy, no-answer, …)."""
    from pincer.observability.failure_codes import NOT_CONNECTED_STATUS
    from pincer.voice.outcome import render_not_connected_report

    info = _outbound_calls.get(call_sid)
    language = info.language if info else ""
    target = (info.target_name or info.target_number) if info else ""
    report = render_not_connected_report(
        target or "unknown", call_sid, str(NOT_CONNECTED_STATUS[status]), language, status
    )
    return await finalize_report(call_sid, report, outcome_code_for(status))


async def reconcile(
    call_sids: list[str],
    fetch_status: Callable[[str], Awaitable[str]],
    has_live_state: Callable[[str], bool],
    *,
    timeout: float = RECONCILE_TIMEOUT_S,
) -> None:
    """Finalize reloaded calls that ended while the process was down.

    - Over without connecting (busy, no-answer, failed, canceled): the usual
      not-connected report.
    - `completed` with no live state here: it was connected when the process
      died, and its transcript went with it — the interrupted report.
    - Still live (queued, ringing, in-progress) or unknown: left for the
      /status callback to finish.

    Bounded by `timeout` as a whole; meant to run as a background task, off
    the startup path. Calls not reached in time wait for their /status.
    """
    try:
        await asyncio.wait_for(_reconcile(call_sids, fetch_status, has_live_state), timeout)
    except TimeoutError:
        logger.warning("Reconciling reloaded calls timed out — the rest wait for their /status callback")
    except Exception:
        logger.exception("Reconciling reloaded calls failed")


async def _reconcile(
    call_sids: list[str],
    fetch_status: Callable[[str], Awaitable[str]],
    has_live_state: Callable[[str], bool],
) -> None:
    from pincer.observability.failure_codes import NOT_CONNECTED_STATUS

    for call_sid in call_sids:
        if call_sid not in _outbound_calls or has_live_state(call_sid):
            continue
        try:
            status = await fetch_status(call_sid)
        except Exception:
            logger.info("Status of reloaded call %s unknown — leaving it to /status", call_sid, exc_info=True)
            continue
        if status in NOT_CONNECTED_STATUS:
            await finalize_not_connected(call_sid, status)
        elif status == "completed":
            await finalize_interrupted(call_sid)


def _reset_for_tests() -> None:
    global _notifier, _session_reporter, _db_path  # noqa: PLW0603
    _notifier = None
    _session_reporter = None
    _db_path = ""
    _outbound_calls.clear()
    _finalizing.clear()
