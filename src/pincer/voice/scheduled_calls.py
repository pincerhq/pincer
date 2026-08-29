"""Outbound calls placed at a time the owner chose.

A scheduled call is an ordinary one-off entry in the cron store whose action
says ``{"type": "voice_call", ...}``. When it comes due the task actor
dispatches here and this places the call through :func:`make_phone_call` — the
same path the dashboard's "call now" and the agent's own tool use, so every
guardrail (do-not-call, quiet hours, daily caps, briefing validation) applies
exactly as it does to a call placed by hand.

Nothing about the call is decided at fire time except *when*: the number, the
briefing and the thread are fixed when the schedule is created, so what goes
out is what the owner wrote.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from croniter import croniter

if TYPE_CHECKING:
    from pincer.config import Settings

logger = logging.getLogger(__name__)

#: `action.type` for a scheduled outbound call.
ACTION_TYPE = "voice_call"

#: The soonest a call may be scheduled. Below this the round trip through the
#: scheduler is slower than the lead time, and "in 0 minutes" is "call now",
#: which has its own endpoint.
MIN_LEAD_MINUTES = 1

#: How far ahead a call may be scheduled. A call parked for months is almost
#: always a mistake, and the briefing behind it will have gone stale.
MAX_LEAD_DAYS = 90


class ScheduledCallError(ValueError):
    """Bad scheduling input. The message is meant for the user, verbatim."""


def one_off_cron(when: datetime) -> str:
    """The cron expression that fires once at `when`.

    Minute, hour, day and month are pinned and weekday is left open — the shape
    :func:`pincer.scheduler.cron.is_one_time_cron` recognises as a one-off.
    """
    return f"{when.minute} {when.hour} {when.day} {when.month} *"


def resolve_when(
    *,
    tz: str,
    run_in_minutes: int | None = None,
    at: str = "",
    now: datetime | None = None,
) -> datetime:
    """Turn "in 20 minutes" or "2026-08-22T09:15" into a concrete local moment.

    Exactly one of the two must be given. A naive `at` is read in the caller's
    timezone, because someone typing 09:15 means 09:15 where they are.
    """
    try:
        tzinfo = ZoneInfo(tz)
    except Exception as e:  # pragma: no cover - config-level mistake
        raise ScheduledCallError(f"Invalid timezone: {tz}") from e

    current = (now or datetime.now(tzinfo)).astimezone(tzinfo)

    if (run_in_minutes is None) == (not at):
        raise ScheduledCallError("Pass exactly one of run_in_minutes or at.")

    if run_in_minutes is not None:
        if run_in_minutes < MIN_LEAD_MINUTES:
            raise ScheduledCallError(
                f"Schedule the call at least {MIN_LEAD_MINUTES} minute ahead, "
                "or place it now instead."
            )
        target = current + timedelta(minutes=run_in_minutes)
    else:
        try:
            parsed = datetime.fromisoformat(at.replace(" ", "T"))
        except ValueError as e:
            raise ScheduledCallError(f"Could not read the time: {at}") from e
        target = parsed if parsed.tzinfo else parsed.replace(tzinfo=tzinfo)
        target = target.astimezone(tzinfo)

    if target <= current:
        raise ScheduledCallError("That moment has already passed.")
    if target - current > timedelta(days=MAX_LEAD_DAYS):
        raise ScheduledCallError(f"Schedule the call within the next {MAX_LEAD_DAYS} days.")

    # The cron expression has minute resolution, so seconds would be a promise
    # the scheduler cannot keep.
    return target.replace(second=0, microsecond=0)


def fires_at(cron_expr: str, tz: str, now: datetime | None = None) -> str:
    """When a stored one-off will actually fire, as UTC ISO — for the API."""
    tzinfo = ZoneInfo(tz)
    base = (now or datetime.now(tzinfo)).astimezone(tzinfo)
    return croniter(cron_expr, base).get_next(datetime).astimezone(UTC).isoformat()


def build_action(
    *,
    target_number: str,
    purpose: str,
    target_name: str = "",
    language: str = "",
    instructions: str = "",
    thread_id: str = "",
    scheduled_for: str = "",
) -> dict[str, Any]:
    """The action payload stored on the schedule row."""
    return {
        "type": ACTION_TYPE,
        "target_number": target_number,
        "purpose": purpose,
        "target_name": target_name,
        "language": language,
        "instructions": instructions,
        "thread_id": thread_id,
        # Kept for the UI: the cron expression alone cannot say which year, and
        # a listing should not have to re-derive the owner's intent.
        "scheduled_for": scheduled_for,
    }


def make_scheduled_call_handler(settings: Settings) -> Any:
    """Build the CronScheduler action handler for ``voice_call``."""

    async def _handler(pincer_user_id: str, action: dict[str, Any], channel: str) -> str | None:
        from pincer.voice.outbound import make_phone_call

        number = str(action.get("target_number", "")).strip()
        purpose = str(action.get("purpose", "")).strip()
        if not number or not purpose:
            logger.error("Scheduled call skipped: action is missing number or purpose")
            return "A scheduled call could not be placed: it had no number or no purpose."

        logger.info("Scheduled call firing: to=%s user=%s", number, pincer_user_id)
        try:
            result = await make_phone_call(
                target_number=number,
                purpose=purpose,
                instructions=str(action.get("instructions", "")),
                language=str(action.get("language", "")),
                context={"user_id": pincer_user_id, "channel": channel},
                target_name=str(action.get("target_name", "")),
                thread_id=str(action.get("thread_id", "")),
                source="scheduler",
            )
        except Exception:
            logger.exception("Scheduled call failed: to=%s", number)
            # Told, not swallowed: a call that was promised and did not happen
            # is exactly what the owner needs to hear about.
            return f"The scheduled call to {number} could not be placed."

        # `make_phone_call` reports its own guardrail refusals as "Error: …";
        # they are the user's answer, so they go to the user.
        if result.startswith("Error"):
            return f"The scheduled call to {number} was refused: {result.removeprefix('Error:').strip()}"
        return f"Placing the scheduled call to {number}: {purpose}"

    return _handler


__all__ = [
    "ACTION_TYPE",
    "MAX_LEAD_DAYS",
    "MIN_LEAD_MINUTES",
    "ScheduledCallError",
    "build_action",
    "fires_at",
    "make_scheduled_call_handler",
    "one_off_cron",
    "resolve_when",
]
