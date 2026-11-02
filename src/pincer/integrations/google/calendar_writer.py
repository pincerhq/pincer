"""
Calendar writer — a write counts only when Google hands back the stored event,
and a retried write never duplicates or falsely fails.

- **Create** sets Google's client-supplied event ID: a base32hex hash of (dedup
  seed, calendar id, summary, start, end). The seed is the chat turn's ID, or an
  explicit idempotency key (the voice booking flow's per-task key). Google
  refuses a second insert with the same ID (409), so the retry wrapper resending
  an insert whose first response was lost, or the model re-calling create in the
  same turn, finds the existing event instead of making another — atomically on
  Google's side, unlike a check-then-insert lookup. Dedup covers EXACT re-calls
  only: same seed, same calendar id as written ("primary" is not resolved to an
  address), same title (whitespace-normalised), same start/end instants.
- An ID whose event was deleted stays reserved by Google (409 again), and a
  purged one may no longer even be served (the GET after the 409 is 404/410).
  Both mean "not live": the next deterministic ID is tried, a bounded number of
  times, each through the same 409 → GET handling.
- The insert/update response *is* Google's stored copy, so tools report its
  times rather than the requested values; no read-back GET is needed. A
  response without an ``id`` proves nothing: it raises, the agent marks the
  result ``is_error``, and the wording says the event may or may not exist.
- An insert that fails without an HTTP status (a timeout or connection reset,
  never retried) may still have committed: the deterministic ID is looked up,
  and a live event there is reported as created rather than raising (which
  made the model create again).
- **Delete/move** retried after a lost-but-committed first attempt get 404/410;
  on a retry the event's actual state decides. A delete whose FIRST attempt is
  410 (Google's "already deleted") is a success, reported as such; a first 404
  (wrong ID, no access) stays an error.
- Every write logs one ``calendar_write`` line (operation, turn, call, calendar,
  event ID, stored start/end, timezone, dedup hit).
"""

from __future__ import annotations

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
#: "Gone": the event existed and is deleted (unlike 404, which may be a wrong ID).
_DELETED = 410
#: How many deterministic IDs a create tries when earlier ones belong to deleted events.
MAX_ID_GENERATIONS = 3
ALL_IDS_TAKEN_MESSAGE = (
    "Could not create the event: earlier events with exactly these details were deleted, and Google keeps "
    "their IDs reserved. Change a detail (for example the title) and try again."
)


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


class _AttemptError(Exception):
    """A call under the retry wrapper failed; carries how many attempts were made."""

    def __init__(self, error: Exception, attempts: int) -> None:
        super().__init__(str(error))
        self.error = error
        self.attempts = attempts


@dataclass(frozen=True)
class CreateResult:
    event: dict[str, Any]
    #: True when the event already existed (409) and was fetched, not created now.
    existed: bool
    #: True when the insert got no answer (a reset connection) and a lookup found the event:
    #: this call's write or an earlier one with the same ID — which, the lookup cannot tell.
    recovered: bool = False


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
        " ".join(str(body.get("summary") or "").split()),
        _instant_key(body.get("start") or {}),
        _instant_key(body.get("end") or {}),
    ]
    digest = hashlib.sha256("\x1f".join(parts).encode()).digest()
    return base64.b32hexencode(digest).decode().rstrip("=").lower()


def _confirmed(saved: Any) -> dict[str, Any]:
    if not isinstance(saved, dict) or not saved.get("id"):
        raise CalendarWriteNotConfirmedError
    return saved


def log_write(
    op: str,
    trace: Trace,
    calendar_id: str,
    event: dict[str, Any] | None,
    *,
    event_id: str = "",
    existed: bool = False,
    confirmed: bool = True,
) -> None:
    ev = event or {}
    start, end = ev.get("start") or {}, ev.get("end") or {}
    logger.info(
        "calendar_write op=%s turn=%s call=%s calendar=%s event=%s start=%s end=%s tz=%s dedup=%s confirmed=%s",
        op,
        trace.turn_id or "-",
        trace.call_id or "-",
        calendar_id,
        ev.get("id") or event_id or "-",
        start.get("dateTime") or start.get("date") or "-",
        end.get("dateTime") or end.get("date") or "-",
        start.get("timeZone") or "-",
        existed,
        confirmed,
    )


def _logged_and_confirmed(
    op: str, trace: Trace, calendar_id: str, saved: Any, *, event_id: str, existed: bool = False
) -> dict[str, Any]:
    """Log the write attempt first (``confirmed=False`` when Google's response has no ID), then
    confirm it: an unconfirmed write is still traceable in the write log."""
    response = saved if isinstance(saved, dict) else None
    confirmed = bool(response and response.get("id"))
    log_write(op, trace, calendar_id, response, event_id=event_id, existed=existed, confirmed=confirmed)
    return _confirmed(saved)


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
    """Insert ``body`` under its client-supplied ID; a live event with that ID is returned instead.

    ``existed`` is True when a 409 arrived on the FIRST attempt (an earlier call
    made the event — same turn or same key). A 409 on the wrapper's own retry
    means this call's first attempt committed and only its response was lost,
    so it counts as created now. (Wording edge: if a layer below the wrapper
    retried invisibly, a first-attempt 409 can be this call's own write; it is
    then reported as "already created", which is still one event, correctly.)
    """
    trace = trace or Trace()
    event_id = client_event_id(seed, calendar_id, body)
    for _generation in range(MAX_ID_GENERATIONS):
        outcome = await _insert_or_fetch(svc, calendar_id, body, event_id, insert_kwargs)
        if outcome is not None:
            saved, existed, recovered = outcome
            break
        # That ID belongs to a deleted (or purged) event: derive the next one.
        event_id = client_event_id(f"{seed}\x1fafter-cancelled:{event_id}", calendar_id, body)
    else:
        raise RuntimeError(ALL_IDS_TAKEN_MESSAGE)
    event = _logged_and_confirmed("create", trace, calendar_id, saved, event_id=event_id, existed=existed)
    return CreateResult(event=event, existed=existed, recovered=recovered)


async def _insert_or_fetch(
    svc: Any, calendar_id: str, body: dict[str, Any], event_id: str, insert_kwargs: dict[str, Any]
) -> tuple[Any, bool, bool] | None:
    """Insert under ``event_id``: (saved, existed, recovered), or None when the ID is taken by a non-live event."""
    payload = {**body, "id": event_id}
    try:
        saved, _attempts = await _attempted(
            lambda: svc.events().insert(calendarId=calendar_id, body=payload, **insert_kwargs).execute()
        )
        return saved, False, False
    except _AttemptError as failed:
        status = _status(failed.error)
        if status is None:
            return await _committed_despite(failed, svc, calendar_id, event_id)
        if status != _CONFLICT:
            raise failed.error from None
        conflict_on_first_attempt = failed.attempts < 2
    try:
        existing = await _get(svc, calendar_id, event_id)
    except Exception as exc:
        if _status(exc) in _GONE:
            return None  # reserved but no longer served: treat like a deleted event
        raise
    if existing.get("status") == "cancelled":
        return None
    return existing, conflict_on_first_attempt, False


async def _committed_despite(
    failed: _AttemptError, svc: Any, calendar_id: str, event_id: str
) -> tuple[dict[str, Any], bool, bool]:
    """A non-HTTP insert failure: the event under ``event_id`` if live (created now, recovered), else the error."""
    try:
        existing = await _live_on(svc, calendar_id, event_id)
    except Exception:
        raise failed.error from None
    if existing is None:
        raise failed.error from None
    logger.info("An insert failed with %s, but the event is in place: created", type(failed.error).__name__)
    return existing, False, True


async def update(
    svc: Any, calendar_id: str, event_id: str, body: dict[str, Any], *, trace: Trace | None = None
) -> dict[str, Any]:
    """Replace event ``event_id`` with ``body`` and return Google's stored copy (raises if unconfirmed)."""
    saved = await with_backoff(
        lambda: svc.events().update(calendarId=calendar_id, eventId=event_id, body=body).execute()
    )
    return _logged_and_confirmed("update", trace or Trace(), calendar_id, saved, event_id=event_id)


async def patch(
    svc: Any,
    calendar_id: str,
    event_id: str,
    body: dict[str, Any],
    *,
    op: str,
    trace: Trace | None = None,
    **patch_kwargs: Any,
) -> dict[str, Any]:
    """Patch event ``event_id`` (accept, decline, add Meet) and return Google's stored copy (raises if unconfirmed)."""
    saved = await with_backoff(
        lambda: svc.events().patch(calendarId=calendar_id, eventId=event_id, body=body, **patch_kwargs).execute()
    )
    return _logged_and_confirmed(op, trace or Trace(), calendar_id, saved, event_id=event_id)


async def _attempted(fn: Any) -> tuple[Any, int]:
    """Run ``fn`` under the retry wrapper; return its result and how many attempts were made.

    A failure is raised as ``_AttemptError`` carrying the original error and the count.
    """
    attempts = 0

    def _counted() -> Any:
        nonlocal attempts
        attempts += 1
        return fn()

    try:
        return await with_backoff(_counted), attempts
    except Exception as exc:
        raise _AttemptError(exc, attempts) from exc


async def _recovered_after_retry(failed: _AttemptError, committed: Any) -> Any:
    """For delete/move: a 404/410 on a RETRY (never the first attempt) may mean the
    first attempt committed and only its response was lost. ``committed()`` checks
    the real state and returns a truthy result if so; otherwise the error stands."""
    if _status(failed.error) not in _GONE or failed.attempts < 2:
        raise failed.error from None
    result = await committed()
    if not result:
        raise failed.error from None
    logger.info("A retried calendar write got %s, but the change is in place: success", _status(failed.error))
    return result


async def delete(svc: Any, calendar_id: str, event_id: str, *, trace: Trace | None = None) -> bool:
    """Delete an event; True when it was already deleted (a 410 on the first attempt).

    A 404/410 on a retry is checked against the event's real state: gone or
    ``cancelled`` means the lost first attempt committed (deleted now).
    """
    already_gone = recovered = False
    try:
        await _attempted(lambda: svc.events().delete(calendarId=calendar_id, eventId=event_id).execute())
    except _AttemptError as failed:
        if failed.attempts < 2 and _status(failed.error) == _DELETED:
            already_gone = True
        else:
            recovered = await _recovered_after_retry(failed, lambda: _is_gone(svc, calendar_id, event_id))
    log_write("delete", trace or Trace(), calendar_id, None, event_id=event_id, existed=already_gone or recovered)
    return already_gone


async def _is_gone(svc: Any, calendar_id: str, event_id: str) -> bool:
    try:
        event = await _get(svc, calendar_id, event_id)
    except Exception as exc:
        if _status(exc) in _GONE:
            return True
        raise
    return isinstance(event, dict) and event.get("status") == "cancelled"


async def _live_on(svc: Any, calendar_id: str, event_id: str) -> dict[str, Any] | None:
    try:
        event = await _get(svc, calendar_id, event_id)
    except Exception as exc:
        if _status(exc) in _GONE:
            return None
        raise
    return event if isinstance(event, dict) and event.get("status") != "cancelled" else None


async def move(
    svc: Any, calendar_id: str, event_id: str, destination: str, *, trace: Trace | None = None
) -> dict[str, Any]:
    """Move an event to ``destination`` and return Google's stored copy (raises if unconfirmed).
    A 404/410 on a retry is checked against the destination: the event being there
    means the lost first attempt committed."""
    recovered = False
    try:
        moved, _attempts = await _attempted(
            lambda: svc.events().move(calendarId=calendar_id, eventId=event_id, destination=destination).execute()
        )
    except _AttemptError as failed:
        moved = await _recovered_after_retry(failed, lambda: _live_on(svc, destination, event_id))
        recovered = True
    return _logged_and_confirmed("move", trace or Trace(), destination, moved, event_id=event_id, existed=recovered)
