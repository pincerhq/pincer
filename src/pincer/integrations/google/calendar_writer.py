"""
Calendar writer — a write counts only when Google hands back the stored event.

The insert/update response *is* Google's stored copy, so the tools report its
times rather than the values they asked for, and no read-back GET is needed.
A response without an ``id`` proves nothing was confirmed: that raises, the
agent marks the tool result ``is_error``, and the wording says the event may
or may not exist — never that it was created or updated.

(Deduplicated creates via a client-supplied event ID build on this module.)
"""

from __future__ import annotations

from typing import Any

from pincer.integrations.google.quota import with_backoff

NOT_CONFIRMED_MESSAGE = (
    "Calendar write not confirmed: Google's response carried no event ID, so the event may or may not "
    "exist. Check the calendar (search or list) before trying again."
)


class CalendarWriteNotConfirmedError(RuntimeError):
    """Google answered a write without the stored event's ID."""

    def __init__(self) -> None:
        super().__init__(NOT_CONFIRMED_MESSAGE)


def _confirmed(saved: Any) -> dict[str, Any]:
    if not isinstance(saved, dict) or not saved.get("id"):
        raise CalendarWriteNotConfirmedError
    return saved


async def create(svc: Any, calendar_id: str, body: dict[str, Any], **insert_kwargs: Any) -> dict[str, Any]:
    """Insert ``body`` and return Google's stored copy (raises if unconfirmed)."""
    saved = await with_backoff(
        lambda: svc.events().insert(calendarId=calendar_id, body=body, **insert_kwargs).execute()
    )
    return _confirmed(saved)


async def update(svc: Any, calendar_id: str, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Replace event ``event_id`` with ``body`` and return Google's stored copy (raises if unconfirmed)."""
    saved = await with_backoff(
        lambda: svc.events().update(calendarId=calendar_id, eventId=event_id, body=body).execute()
    )
    return _confirmed(saved)
