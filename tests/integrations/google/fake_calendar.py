"""
A stateful fake of the Google Calendar v3 service, for calendar behaviour tests.

MagicMock chains accept any arguments, which is how an offset-less ``timeMin``
reached production and failed with a real HTTP 400. This fake keeps events per
calendar, knows each calendar's ``timeZone``, and enforces the API rules the
tools depend on:

- ``timeMin``/``timeMax`` (events.list, freebusy.query) must be RFC 3339 with an
  offset or ``Z`` — otherwise HTTP 400, like Google.
- An event ``dateTime`` without an offset is read in the event's ``timeZone``;
  without either it is a 400 ("Missing time zone definition").
- Stored ``dateTime`` comes back with the CALENDAR's offset even when the event
  has its own ``timeZone`` (which is kept) — measured on a real Europe/Kiev
  calendar on 2026-11-01: a 10:00 America/New_York event read back as
  ``{'dateTime': '2026-11-21T17:00:00+02:00', 'timeZone': 'America/New_York'}``.
- A bare date in ``dateTime`` is a 400; all-day events span midnight to
  midnight in the calendar's zone.
- An inverted range (timeMax before timeMin) lists nothing, without an error.
- An event whose end is not after its start is a 400 ("time range is empty").
- A write's response is the stored copy (``on_store`` can alter what is stored,
  ``drop_id_on_write`` strips the ``id`` from the response).
- Insert honours a client-supplied ``id`` (base32hex, 5–1024 chars, else 400);
  an ID already used on the calendar — even by a deleted event — is a 409.
- Delete leaves a ``cancelled`` tombstone, like Google: get still returns it,
  list/free-busy skip it, a second delete is a 410.
- ``fail_next(method, "before-commit" | "after-commit")`` makes the next call of
  ``events.insert`` / ``events.delete`` / ``events.move`` raise a 503 — before
  changing anything, or after the change was made (a lost response).

Errors are real ``googleapiclient.errors.HttpError`` instances so the tools'
retry and error paths behave as they would against Google.
"""

from __future__ import annotations

import copy
import itertools
import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httplib2
from googleapiclient.errors import HttpError

if TYPE_CHECKING:
    from collections.abc import Callable


_EVENT_ID_RE = re.compile(r"^[0-9a-v]{5,1024}$")


def http_error(status: int, message: str) -> HttpError:
    resp = httplib2.Response({"status": status})
    resp.reason = message
    body = json.dumps({"error": {"code": status, "message": message, "errors": [{"message": message}]}})
    return HttpError(resp, body.encode())


class _Request:
    def __init__(self, fn: Any) -> None:
        self._fn = fn

    def execute(self) -> Any:
        return self._fn()


def _parse_bound(name: str, value: Any) -> datetime:
    """Google's RFC 3339 check: a bound without an offset is a 400."""
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        raise http_error(400, f"Bad Request: invalid {name} {value!r}") from None
    if parsed.tzinfo is None:
        raise http_error(400, f"Bad Request: {name} {value!r} has no timezone offset")
    return parsed


class _Calendars:
    def __init__(self, fake: FakeCalendarService) -> None:
        self._fake = fake

    def get(self, calendarId: str) -> _Request:  # noqa: N803 - Google's parameter name
        def _run() -> dict[str, Any]:
            self._fake.calls.append(("calendars.get", {"calendarId": calendarId}))
            if self._fake.calendar_get_error is not None:
                raise http_error(self._fake.calendar_get_error, "calendar lookup failed")
            cal = self._fake.calendar(calendarId)
            return {"id": calendarId, "summary": calendarId, "timeZone": cal["timeZone"]}

        return _Request(_run)


class _Events:
    def __init__(self, fake: FakeCalendarService) -> None:
        self._fake = fake

    def list(self, calendarId: str, **kwargs: Any) -> _Request:  # noqa: N803
        def _run() -> dict[str, Any]:
            self._fake.calls.append(("events.list", {"calendarId": calendarId, **kwargs}))
            cal = self._fake.calendar(calendarId)
            t_min = _parse_bound("timeMin", kwargs["timeMin"]) if "timeMin" in kwargs else None
            t_max = _parse_bound("timeMax", kwargs["timeMax"]) if "timeMax" in kwargs else None
            if t_min is not None and t_max is not None and t_max <= t_min:
                # Measured live: an inverted range is not a 400, just an empty result.
                return {"items": [], "timeZone": cal["timeZone"]}
            items = []
            for event in cal["events"].values():
                if event.get("status") == "cancelled":
                    continue
                start, end = self._fake.instants(event, cal["timeZone"])
                if t_min is not None and end <= t_min:
                    continue
                if t_max is not None and start >= t_max:
                    continue
                query = str(kwargs.get("q") or "").lower()
                if query:
                    text = " ".join(str(event.get(k, "")) for k in ("summary", "description", "location")).lower()
                    if query not in text:
                        continue
                prop = kwargs.get("privateExtendedProperty")
                if prop:
                    key, _, value = str(prop).partition("=")
                    if event.get("extendedProperties", {}).get("private", {}).get(key) != value:
                        continue
                items.append(event)
            items.sort(key=lambda e: self._fake.instants(e, cal["timeZone"])[0])
            limit = int(kwargs.get("maxResults") or 250)
            return {"items": copy.deepcopy(items[:limit]), "timeZone": cal["timeZone"]}

        return _Request(_run)

    def get(self, calendarId: str, eventId: str) -> _Request:  # noqa: N803
        def _run() -> dict[str, Any]:
            self._fake.calls.append(("events.get", {"calendarId": calendarId, "eventId": eventId}))
            return copy.deepcopy(self._fake.event(calendarId, eventId))

        return _Request(_run)

    def insert(self, calendarId: str, body: dict[str, Any], **kwargs: Any) -> _Request:  # noqa: N803
        def _run() -> dict[str, Any]:
            self._fake.calls.append(
                ("events.insert", {"calendarId": calendarId, "body": copy.deepcopy(body), **kwargs})
            )
            cal = self._fake.calendar(calendarId)
            self._fake.fault("events.insert", "before-commit")
            event_id = body.get("id") or f"evt{next(self._fake._ids)}"
            if not _EVENT_ID_RE.match(str(event_id)):
                raise http_error(400, "Invalid resource id value.")
            if event_id in cal["events"]:
                raise http_error(409, "The requested identifier already exists.")
            event = self._fake.store(cal, {**copy.deepcopy(body), "id": event_id})
            cal["events"][event_id] = event
            response = self._fake.write_response(event)
            self._fake.fault("events.insert", "after-commit")
            return response

        return _Request(_run)

    def update(self, calendarId: str, eventId: str, body: dict[str, Any], **kwargs: Any) -> _Request:  # noqa: N803
        def _run() -> dict[str, Any]:
            self._fake.calls.append(
                ("events.update", {"calendarId": calendarId, "eventId": eventId, "body": copy.deepcopy(body), **kwargs})
            )
            cal = self._fake.calendar(calendarId)
            self._fake.event(calendarId, eventId)
            event = self._fake.store(cal, {**copy.deepcopy(body), "id": eventId})
            cal["events"][eventId] = event
            return self._fake.write_response(event)

        return _Request(_run)

    def delete(self, calendarId: str, eventId: str, **kwargs: Any) -> _Request:  # noqa: N803
        def _run() -> None:
            self._fake.calls.append(("events.delete", {"calendarId": calendarId, "eventId": eventId, **kwargs}))
            self._fake.fault("events.delete", "before-commit")
            event = self._fake.event(calendarId, eventId)
            if event.get("status") == "cancelled":
                raise http_error(410, "Resource has been deleted")
            event["status"] = "cancelled"
            self._fake.fault("events.delete", "after-commit")

        return _Request(_run)

    def move(self, calendarId: str, eventId: str, destination: str, **kwargs: Any) -> _Request:  # noqa: N803
        def _run() -> dict[str, Any]:
            self._fake.calls.append(
                ("events.move", {"calendarId": calendarId, "eventId": eventId, "destination": destination, **kwargs})
            )
            self._fake.fault("events.move", "before-commit")
            source = self._fake.calendar(calendarId)
            target = self._fake.calendar(destination)
            event = source["events"].get(eventId)
            if event is None or event.get("status") == "cancelled":
                raise http_error(404, f"Not Found: event {eventId}")
            del source["events"][eventId]
            target["events"][eventId] = event
            response = copy.deepcopy(event)
            self._fake.fault("events.move", "after-commit")
            return response

        return _Request(_run)


class _FreeBusy:
    def __init__(self, fake: FakeCalendarService) -> None:
        self._fake = fake

    def query(self, body: dict[str, Any]) -> _Request:
        def _run() -> dict[str, Any]:
            self._fake.calls.append(("freebusy.query", {"body": copy.deepcopy(body)}))
            t_min = _parse_bound("timeMin", body.get("timeMin"))
            t_max = _parse_bound("timeMax", body.get("timeMax"))
            calendars: dict[str, Any] = {}
            for item in body.get("items", []):
                cal_id = item["id"]
                cal = self._fake.calendar_data.get(cal_id)
                if cal is None:
                    calendars[cal_id] = {"errors": [{"domain": "global", "reason": "notFound"}], "busy": []}
                    continue
                busy = []
                for event in cal["events"].values():
                    if event.get("status") == "cancelled":
                        continue
                    start, end = self._fake.instants(event, cal["timeZone"])
                    if end > t_min and start < t_max:
                        busy.append(
                            {
                                "start": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "end": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            }
                        )
                busy.sort(key=lambda s: s["start"])
                calendars[cal_id] = {"busy": busy}
            return {"timeMin": body["timeMin"], "timeMax": body["timeMax"], "calendars": calendars}

        return _Request(_run)


class FakeCalendarService:
    """Stateful stand-in for ``build("calendar", "v3")``."""

    def __init__(self, calendars: dict[str, str] | None = None) -> None:
        # calendar id → {"timeZone": str, "events": {event id: event}}
        self.calendar_data: dict[str, dict[str, Any]] = {}
        for cal_id, tz in (calendars or {"primary": "UTC"}).items():
            self.add_calendar(cal_id, tz)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        #: When set, calendars.get raises an HttpError with this status.
        self.calendar_get_error: int | None = None
        #: Called with each event stored by insert/update (may alter it, like
        #: Google normalising a value) before the response is built.
        self.on_store: Callable[[dict[str, Any]], None] | None = None
        #: When True, insert/update store the event but answer without its id.
        self.drop_id_on_write = False
        #: method → queued fault phases ("before-commit" / "after-commit"), see fail_next.
        self.faults: dict[str, list[str]] = {}
        self._ids = itertools.count(1)

    # ── API surface ──────────────────────────────────────────────────────
    def calendars(self) -> _Calendars:  # noqa: D102 - mirrors the API
        return _Calendars(self)

    def events(self) -> _Events:  # noqa: D102
        return _Events(self)

    def freebusy(self) -> _FreeBusy:  # noqa: D102
        return _FreeBusy(self)

    # ── Test helpers ─────────────────────────────────────────────────────
    def add_calendar(self, calendar_id: str, time_zone: str) -> None:
        self.calendar_data[calendar_id] = {"timeZone": time_zone, "events": {}}

    def calendar(self, calendar_id: str) -> dict[str, Any]:
        cal = self.calendar_data.get(calendar_id)
        if cal is None:
            raise http_error(404, f"Not Found: calendar {calendar_id}")
        return cal

    def event(self, calendar_id: str, event_id: str) -> dict[str, Any]:
        event = self.calendar(calendar_id)["events"].get(event_id)
        if event is None:
            raise http_error(404, f"Not Found: event {event_id}")
        return event

    def seed_event(self, calendar_id: str, summary: str, start: str, end: str, time_zone: str | None = None) -> str:
        """Put an event straight into the calendar (as if made elsewhere)."""
        start_field: dict[str, Any] = {"dateTime": start}
        end_field: dict[str, Any] = {"dateTime": end}
        if time_zone:
            start_field["timeZone"] = time_zone
            end_field["timeZone"] = time_zone
        cal = self.calendar(calendar_id)
        event_id = f"evt{next(self._ids)}"
        cal["events"][event_id] = self.store(
            cal, {"id": event_id, "summary": summary, "start": start_field, "end": end_field}
        )
        return event_id

    def seed_all_day(self, calendar_id: str, summary: str, first_day: str, end_day_exclusive: str) -> str:
        """An all-day event (Google's end date is exclusive)."""
        cal = self.calendar(calendar_id)
        event_id = f"evt{next(self._ids)}"
        cal["events"][event_id] = self.store(
            cal,
            {"id": event_id, "summary": summary, "start": {"date": first_day}, "end": {"date": end_day_exclusive}},
        )
        return event_id

    def write_response(self, event: dict[str, Any]) -> dict[str, Any]:
        if self.on_store is not None:
            self.on_store(event)
        response = copy.deepcopy(event)
        if self.drop_id_on_write:
            response.pop("id", None)
        return response

    def events_on(self, calendar_id: str) -> list[dict[str, Any]]:
        """Live (not cancelled) events on the calendar."""
        return [e for e in self.calendar(calendar_id)["events"].values() if e.get("status") != "cancelled"]

    def fail_next(self, method: str, phase: str) -> None:
        """The next ``method`` call raises a 503 ``before-commit`` or ``after-commit``."""
        assert phase in ("before-commit", "after-commit"), phase
        self.faults.setdefault(method, []).append(phase)

    def fault(self, method: str, phase: str) -> None:
        queued = self.faults.get(method)
        if queued and queued[0] == phase:
            queued.pop(0)
            raise http_error(503, f"Backend Error ({phase})")

    def start_of(self, calendar_id: str, event_id: str) -> datetime:
        return self.instants(self.event(calendar_id, event_id), self.calendar(calendar_id)["timeZone"])[0]

    def sent(self, method: str) -> list[dict[str, Any]]:
        """The keyword arguments of every call to ``method`` (e.g. "events.list")."""
        return [kwargs for name, kwargs in self.calls if name == method]

    # ── Google's storage rules ───────────────────────────────────────────
    def store(self, cal: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
        for key in ("start", "end"):
            event[key] = self._normalize(cal["timeZone"], key, event.get(key) or {})
        start, end = self.instants(event, cal["timeZone"])
        if end <= start:
            # Google: "The specified time range is empty." (e.g. a moved start past the old end)
            raise http_error(400, "The specified time range is empty.")
        event.setdefault("status", "confirmed")
        event["htmlLink"] = f"https://calendar.google.com/calendar/event?eid={event['id']}"
        return event

    @staticmethod
    def _normalize(cal_tz: str, key: str, field: dict[str, Any]) -> dict[str, Any]:
        if "date" in field:
            return dict(field)
        raw = field.get("dateTime")
        tz_name = field.get("timeZone")
        if "T" not in str(raw):
            # Google rejects a bare date (or anything without a time) in dateTime.
            raise http_error(400, f"Bad Request: invalid {key}.dateTime {raw!r}")
        try:
            parsed = datetime.fromisoformat(str(raw))
        except ValueError:
            raise http_error(400, f"Bad Request: invalid {key}.dateTime {raw!r}") from None
        if parsed.tzinfo is None:
            if not tz_name:
                raise http_error(400, f"Missing time zone definition for {key} time.")
            try:
                parsed = parsed.replace(tzinfo=ZoneInfo(tz_name))
            except (KeyError, ValueError, ZoneInfoNotFoundError):
                raise http_error(400, f"Invalid time zone definition for {key} time.") from None
        stored: dict[str, Any] = {"dateTime": parsed.astimezone(ZoneInfo(cal_tz)).isoformat()}
        if tz_name:
            stored["timeZone"] = tz_name
        return stored

    @staticmethod
    def instants(event: dict[str, Any], time_zone: str) -> tuple[datetime, datetime]:
        """Start/end instants; an all-day date is midnight in the calendar's zone ``time_zone``."""

        def _one(field: dict[str, Any]) -> datetime:
            if "dateTime" in field:
                return datetime.fromisoformat(field["dateTime"])
            return datetime.fromisoformat(field["date"]).replace(tzinfo=ZoneInfo(time_zone))

        return _one(event["start"]), _one(event["end"])


class FakeCalendarFactory:
    """A ``GoogleServiceFactory`` stand-in serving one fake Calendar service."""

    def __init__(self, service: FakeCalendarService) -> None:
        self.service = service

    async def get(self, service_name: str) -> Any:
        if service_name != "calendar":
            raise ValueError(f"FakeCalendarFactory serves only 'calendar', not {service_name!r}")
        return self.service
