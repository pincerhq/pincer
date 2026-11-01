"""
Calendar writer — a write counts only when Google hands back the stored event,
and a retried write never duplicates or falsely fails.

- **Create** sets Google's client-supplied event ID: a base32hex hash of (dedup
  seed, calendar id, summary, start, end). The seed is the chat turn's ID, or an
  explicit idempotency key (the voice booking flow's per-task key). Google
  refuses a second insert with the same ID (409), so the retry wrapper resending
  an insert whose first response was lost, or the model re-calling create in the
  same turn, finds the existing event instead of making another — atomically on
  Google's side, unlike a check-then-insert lookup.
- The insert/update response *is* Google's stored copy, so tools report its
  times rather than the requested values; no read-back GET is needed. A
  response without an ``id`` proves nothing: it raises, the agent marks the
  result ``is_error``, and the wording says the event may or may not exist.
- **Delete/move** retried after a lost-but-committed first attempt get 404/410;
  on a retry (never on the first attempt) the event's actual state decides.
- Every write logs one ``calendar_write`` line (operation, turn, call, calendar,
  event ID, stored start/end, timezone, dedup hit).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pincer.integrations.google.quota import with_backoff

logger = logging.getLogger(__name__)

NOT_CONFIRMED_MESSAGE = (
    "Calendar write not confirmed: Google's response carried no event ID, so the event may or may not "
    "exist. Check the calendar (search or list) before trying again."
)
#: Google's "already exists" (insert) and "not found"/"gone" statuses.
_CONFLICT = 409
_GONE = (404, 410)


class CalendarWriteNotConfirmedError(RuntimeError):
    """Google answered a write without the stored event's ID."""

    def __init__(self) -> None:
        super().__init__(NOT_CONFIRMED_MESSAGE)


@dataclass(frozen=True)
class Trace:
    """Which turn and tool call a write belongs to (for the ``calendar_write`` log line)."""

    turn_id: str = ""
    call_id: str = ""

    @classmethod
    def from_context(cls, context: dict[str, Any] | None) -> Trace:
        ctx = context or {}
        return cls(turn_id=str(ctx.get("turn_id") or ""), call_id=str(ctx.get("tool_call_id") or ""))


@dataclass(frozen=True)
class CreateResult:
    event: dict[str, Any]
    #: True when the event already existed (409) and was fetched, not created now.
    existed: bool


def _status(exc: BaseException) -> int | None:
    try:
        from googleapiclient.errors import HttpError
    except ImportError:  # pragma: no cover - the google extra is installed wherever this runs
        return None
    return exc.status_code if isinstance(exc, HttpError) else None


def _instant_key(field: dict[str, Any]) -> str:
    """A start/end as one canonical string: the UTC instant, or the all-day date."""
    raw = field.get("dateTime")
    if raw:
        try:
            parsed = datetime.fromisoformat(str(raw))
        except ValueError:
            return str(raw)
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC).isoformat()
        return f"{parsed.isoformat()}@{field.get('timeZone', '')}"
    return str(field.get("date") or "")


def client_event_id(seed: str, calendar_id: str, body: dict[str, Any]) -> str:
    """Google client-supplied event ID: base32hex (0-9, a-v), 52 characters.

    Same seed + calendar + summary + start/end instant → same ID, so a retry
    or same-turn re-call collides (409) instead of duplicating.
    """
    parts = [
        seed,
        calendar_id,
        str(body.get("summary") or ""),
        _instant_key(body.get("start") or {}),
        _instant_key(body.get("end") or {}),
    ]
    digest = hashlib.sha256("\x1f".join(parts).encode()).digest()
    return base64.b32hexencode(digest).decode().rstrip("=").lower()


def _confirmed(saved: Any) -> dict[str, Any]:
    if not isinstance(saved, dict) or not saved.get("id"):
        raise CalendarWriteNotConfirmedError
    return saved


def _log_write(
    op: str, trace: Trace, calendar_id: str, event: dict[str, Any] | None, *, event_id: str = "", existed: bool = False
) -> None:
    ev = event or {}
    start, end = ev.get("start") or {}, ev.get("end") or {}
    logger.info(
        "calendar_write op=%s turn=%s call=%s calendar=%s event=%s start=%s end=%s tz=%s dedup=%s",
        op,
        trace.turn_id or "-",
        trace.call_id or "-",
        calendar_id,
        ev.get("id") or event_id or "-",
        start.get("dateTime") or start.get("date") or "-",
        end.get("dateTime") or end.get("date") or "-",
        start.get("timeZone") or "-",
        existed,
    )


async def _get(svc: Any, calendar_id: str, event_id: str) -> dict[str, Any]:
    return await with_backoff(  # type: ignore[no-any-return]
        lambda: svc.events().get(calendarId=calendar_id, eventId=event_id).execute()
    )


async def create(
    svc: Any,
    calendar_id: str,
    body: dict[str, Any],
    *,
    seed: str,
    trace: Trace | None = None,
    **insert_kwargs: Any,
) -> CreateResult:
    """Insert ``body`` under its client-supplied ID; an existing event with that ID is returned instead."""
    trace = trace or Trace()
    event_id = client_event_id(seed, calendar_id, body)
    payload = {**body, "id": event_id}
    existed = False
    try:
        saved, _attempts = await _attempted(
            lambda: svc.events().insert(calendarId=calendar_id, body=payload, **insert_kwargs).execute()
        )
    except Exception as exc:
        if _status(exc) != _CONFLICT:
            raise
        saved = await _get(svc, calendar_id, event_id)
        # A 409 on the wrapper's own retry means our first attempt committed and
        # only its response was lost: this call created it. On the first attempt
        # it means an earlier call (same turn or same key) already did.
        existed = getattr(exc, "pincer_attempts", 1) < 2
        if saved.get("status") == "cancelled":
            # Same details as an event deleted earlier: Google keeps that ID
            # reserved, so this one gets its own (still deterministic) ID.
            retry_id = client_event_id(f"{seed}\x1fafter-cancelled:{event_id}", calendar_id, body)
            payload = {**body, "id": retry_id}
            saved = await with_backoff(
                lambda: svc.events().insert(calendarId=calendar_id, body=payload, **insert_kwargs).execute()
            )
            existed = False
    result = CreateResult(event=_confirmed(saved), existed=existed)
    _log_write("create", trace, calendar_id, result.event, existed=existed)
    return result


async def update(
    svc: Any, calendar_id: str, event_id: str, body: dict[str, Any], *, trace: Trace | None = None
) -> dict[str, Any]:
    """Replace event ``event_id`` with ``body`` and return Google's stored copy (raises if unconfirmed)."""
    saved = await with_backoff(
        lambda: svc.events().update(calendarId=calendar_id, eventId=event_id, body=body).execute()
    )
    confirmed = _confirmed(saved)
    _log_write("update", trace or Trace(), calendar_id, confirmed)
    return confirmed


async def _attempted(fn: Any) -> tuple[Any, int]:
    """Run ``fn`` under the retry wrapper; return its result and how many attempts were made.

    On failure the exception carries the attempt count as ``pincer_attempts``.
    """
    attempts = 0

    def _counted() -> Any:
        nonlocal attempts
        attempts += 1
        return fn()

    try:
        return await with_backoff(_counted), attempts
    except Exception as exc:
        exc.pincer_attempts = attempts  # type: ignore[attr-defined]
        raise


async def delete(svc: Any, calendar_id: str, event_id: str, *, trace: Trace | None = None) -> None:
    """Delete an event. A 404/410 on a retry is checked against the event's real state:
    gone or ``cancelled`` means the lost first attempt committed."""
    try:
        await _attempted(lambda: svc.events().delete(calendarId=calendar_id, eventId=event_id).execute())
    except Exception as exc:
        if _status(exc) not in _GONE or getattr(exc, "pincer_attempts", 1) < 2:
            raise
        if not await _is_gone(svc, calendar_id, event_id):
            raise
        logger.info("Delete of %s returned %s on a retry; the event is gone, so it succeeded", event_id, _status(exc))
    _log_write("delete", trace or Trace(), calendar_id, None, event_id=event_id)


async def _is_gone(svc: Any, calendar_id: str, event_id: str) -> bool:
    try:
        event = await asyncio.to_thread(lambda: svc.events().get(calendarId=calendar_id, eventId=event_id).execute())
    except Exception as exc:
        if _status(exc) in _GONE:
            return True
        raise
    return isinstance(event, dict) and event.get("status") == "cancelled"


async def move(
    svc: Any, calendar_id: str, event_id: str, destination: str, *, trace: Trace | None = None
) -> dict[str, Any]:
    """Move an event to ``destination``. A 404/410 on a retry is checked against the
    destination: the event being there means the lost first attempt committed."""
    try:
        moved, _attempts = await _attempted(
            lambda: svc.events().move(calendarId=calendar_id, eventId=event_id, destination=destination).execute()
        )
    except Exception as exc:
        if _status(exc) not in _GONE or getattr(exc, "pincer_attempts", 1) < 2:
            raise
        try:
            moved = await asyncio.to_thread(
                lambda: svc.events().get(calendarId=destination, eventId=event_id).execute()
            )
        except Exception:
            raise exc from None
        if not isinstance(moved, dict) or moved.get("status") == "cancelled":
            raise
        logger.info(
            "Move of %s returned %s on a retry; it is on %s, so it succeeded", event_id, _status(exc), destination
        )
    _log_write("move", trace or Trace(), destination, moved if isinstance(moved, dict) else None, event_id=event_id)
    return moved if isinstance(moved, dict) else {}
