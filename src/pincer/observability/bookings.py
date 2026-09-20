"""
Booking outcome record (Sprint 9) — the denominator behind the booking SLI.

Sprint 6 knows whether a scheduling call reached a confirmed slot, but that
knowledge only ever reached the initiating user as a chat message. The booking
success rate needs it as data, so every appointment call terminus writes one row
here.

`result` values, deliberately few and stable (they become metric labels and
digest headings):

``confirmed``    a slot was agreed AND the calendar write succeeded
``calendar_failed`` agreed on the call, but the event could not be written —
                 counted separately because it is *our* bug, not a negotiation
                 outcome, and lumping it into `confirmed` would hide a
                 double-booking risk
``out_of_slots`` the callee proposed a time outside the offered candidates
``declined``     the callee engaged but no time was agreed
``unreachable``  never got a human (voicemail, no answer, busy) after all retries

`unreachable` is excluded from the booking rate's denominator by
`golden_signals.booking_success_rate` — a week of voicemails says nothing about
whether the agent can negotiate a time.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pincer.config import Settings

from pincer.services.observability import BookingsService

logger = logging.getLogger(__name__)


class BookingResult(StrEnum):
    CONFIRMED = "confirmed"
    CALENDAR_FAILED = "calendar_failed"
    OUT_OF_SLOTS = "out_of_slots"
    DECLINED = "declined"
    UNREACHABLE = "unreachable"


async def record_booking_outcome(
    settings: Settings | Any,
    *,
    task_id: str,
    result: BookingResult | str,
    call_sid: str = "",
    language: str = "",
    attempts: int = 1,
    detail: str = "",
) -> None:
    """Record one appointment task's final outcome. Never raises.

    Keyed by `task_id`, not `call_sid`: an appointment task can span several
    dial attempts and must count as exactly one booking attempt, or the retry
    policy would quietly deflate the success rate.
    """
    value = str(result)
    try:
        service = await BookingsService.for_path(Path(str(settings.db_path)))
        await service.record_outcome(
            {
                "task_id": task_id,
                "call_sid": call_sid,
                "result": value,
                "language": language,
                "attempts": attempts,
                "detail": detail[:500],
                "recorded_at": datetime.now(UTC).isoformat(),
            }
        )
    except Exception:
        logger.exception("Failed to record booking outcome for task %s", task_id)
        return

    from pincer.observability.metrics import record_booking

    record_booking(result=value, language=language, attempts=attempts)
    logger.info("Booking outcome [task=%s call=%s]: %s (attempt %d)", task_id, call_sid, value, attempts)


async def booking_breakdown(settings: Settings | Any, window_hours: float = 168.0) -> dict[str, int]:
    """`{result: count}` over the window — for the weekly digest."""
    cutoff = (datetime.now(UTC) - timedelta(hours=window_hours)).isoformat()
    try:
        service = await BookingsService.for_path(Path(str(settings.db_path)))
        return await service.counts_by_result(cutoff)
    except Exception:
        logger.debug("booking breakdown query failed", exc_info=True)
        return {}
