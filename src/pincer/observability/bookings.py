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
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import aiosqlite

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pincer.config import Settings

logger = logging.getLogger(__name__)

BOOKING_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS appointment_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    call_sid TEXT DEFAULT '',
    result TEXT NOT NULL,
    language TEXT DEFAULT '',
    attempts INTEGER DEFAULT 1,
    detail TEXT DEFAULT '',
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_appointment_outcomes_recorded ON appointment_outcomes(recorded_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_appointment_outcomes_task ON appointment_outcomes(task_id);
"""


class BookingResult(StrEnum):
    CONFIRMED = "confirmed"
    CALENDAR_FAILED = "calendar_failed"
    OUT_OF_SLOTS = "out_of_slots"
    DECLINED = "declined"
    UNREACHABLE = "unreachable"


@asynccontextmanager
async def _db(settings: Settings | Any) -> AsyncIterator[aiosqlite.Connection]:
    async with aiosqlite.connect(str(settings.db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(BOOKING_TABLE_SQL)
        await conn.commit()
        yield conn


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
        async with _db(settings) as conn:
            await conn.execute(
                "INSERT INTO appointment_outcomes (task_id, call_sid, result, language, attempts, detail, "
                "recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET result=excluded.result, call_sid=excluded.call_sid, "
                "attempts=excluded.attempts, detail=excluded.detail, recorded_at=excluded.recorded_at",
                (task_id, call_sid, value, language, attempts, detail[:500], datetime.now(UTC).isoformat()),
            )
            await conn.commit()
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
        async with _db(settings) as conn:
            rows = await conn.execute_fetchall(
                "SELECT result, COUNT(*) AS n FROM appointment_outcomes WHERE recorded_at >= ? GROUP BY result",
                (cutoff,),
            )
    except Exception:
        logger.debug("booking breakdown query failed", exc_info=True)
        return {}
    return {str(r["result"]): int(r["n"]) for r in rows}
