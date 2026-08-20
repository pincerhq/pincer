"""
Appointment scheduling workflow (Sprint 6) — the DACH SMB business feature.

Command (any channel) → free/busy → 2-3 candidate slots → outbound call that
negotiates ONLY within those candidates → verbal VERIFY → machine-parsed
[APPOINTMENT_CONFIRMED:<ISO>] token → post-call executor writes the Google
Calendar event (attendees invited, idempotent) → honest report.

Enforcement layers for "never commit outside the computed slots":
  1. Prompt: APPOINTMENT_NEGOTIATION_RULES (per language pack) lists the only
     allowed slots and the defer phrasing for counter-proposals.
  2. Code: `process_appointment_response` validates the confirmation token
     against the candidate list — an out-of-list confirmation is stripped and
     replaced with the spoken deferral line, so the commitment is never heard.

The registry (call_sid → AppointmentTask) is in-process, like the
status_notify tracking it rides along with; a restart drops pending retries
together with their context (documented limitation, consistent by design).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from pincer.observability.bookings import BookingResult
from pincer.voice.localtime import get_voice_timezone

if TYPE_CHECKING:
    from pincer.tools.registry import ToolRegistry
    from pincer.voice.engine import CallState
    from pincer.voice.transcript import TranscriptLogger

logger = logging.getLogger(__name__)

MAX_CANDIDATES = 3
SLOT_GRID_MINUTES = 30  # candidate starts snap to :00 / :30
MIN_LEAD_MINUTES = 60  # never offer a slot starting sooner than this
CALENDAR_RETRY_DELAYS_S = (2.0, 5.0)  # executor: initial try + one retry per delay

APPOINTMENT_TOKEN_RE = re.compile(r"\[APPOINTMENT_CONFIRMED:\s*([0-9T:+\-. Zz]+?)\s*\]")

_MEET_KEYWORDS = {"meet", "google meet", "google_meet", "video"}

_WEEKDAYS = {
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "de": ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"],
}

_DAY_KEYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


# ── Task registry ────────────────────────────────────────────────────


@dataclass
class AppointmentTask:
    """One scheduling job, alive across dial attempts of the same job."""

    task_id: str
    user_id: str
    channel: str
    target_number: str
    contact_name: str
    topic: str
    timeframe: str
    duration_minutes: int
    language: str
    attendees: str = ""
    location_or_meet: str = ""
    candidates: list[str] = field(default_factory=list)  # canonical ISO (voice tz)
    status: str = "pending"  # pending | confirmed | out_of_candidates
    agreed_start: str = ""  # canonical ISO from candidates
    proposed_out_of_slot: str = ""  # raw callee counter-proposal we deferred on
    attempts: int = 0  # dial attempts so far (initial call = 1)
    calendar_event_link: str = ""


_tasks: dict[str, AppointmentTask] = {}
_MAX_TRACKED = 200
_retry_tasks: set[asyncio.Task[None]] = set()


def register_appointment(call_sid: str, task: AppointmentTask) -> None:
    if not call_sid:
        return
    _tasks[call_sid] = task
    while len(_tasks) > _MAX_TRACKED:
        _tasks.pop(next(iter(_tasks)), None)


def get_appointment(call_sid: str) -> AppointmentTask | None:
    return _tasks.get(call_sid)


def clear_appointment(call_sid: str) -> AppointmentTask | None:
    return _tasks.pop(call_sid, None)


def _reset_for_tests() -> None:
    _tasks.clear()
    for pending in list(_retry_tasks):
        pending.cancel()
    _retry_tasks.clear()


# ── Config parsing ───────────────────────────────────────────────────


def parse_business_hours(value: str) -> tuple[time, time]:
    """'09:00-17:00' -> (time(9), time(17)); malformed input falls back."""
    try:
        start_s, end_s = str(value or "").strip().split("-")
        start_h, start_m = (int(p) for p in start_s.strip().split(":"))
        end_h, end_m = (int(p) for p in end_s.strip().split(":"))
        start, end = time(start_h, start_m), time(end_h, end_m)
        if start < end:
            return start, end
    except (ValueError, AttributeError):
        pass
    return time(9, 0), time(17, 0)


def parse_business_days(value: str) -> set[int]:
    """'mon,tue,fri' -> {0, 1, 4} (Monday=0); malformed input -> Mon-Fri."""
    days = {_DAY_KEYS[p.strip().lower()[:3]] for p in str(value or "").split(",") if p.strip().lower()[:3] in _DAY_KEYS}
    return days or {0, 1, 2, 3, 4}


def resolve_timeframe(timeframe: str, now: datetime) -> tuple[datetime, datetime]:
    """User timeframe -> (window_start, window_end), tz-aware in `now`'s zone.

    Supported: 'tomorrow', 'next_week' (next Mon-Sun), 'this_week',
    'YYYY-MM-DD/YYYY-MM-DD' ISO range. Anything else -> next 7 days.
    """
    tf = str(timeframe or "").strip().lower()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if tf == "tomorrow":
        start = day_start + timedelta(days=1)
        return start, start + timedelta(days=1)
    if tf == "next_week":
        next_monday = day_start + timedelta(days=7 - day_start.weekday())
        return next_monday, next_monday + timedelta(days=7)
    if tf == "this_week":
        return now, day_start + timedelta(days=7 - day_start.weekday())
    if "/" in tf:
        try:
            start_s, end_s = tf.split("/", 1)
            start = datetime.fromisoformat(start_s.strip())
            end = datetime.fromisoformat(end_s.strip())
            if start.tzinfo is None:
                start = start.replace(tzinfo=now.tzinfo)
            if end.tzinfo is None:
                end = end.replace(tzinfo=now.tzinfo)
            # A date-only end means "through that day"
            if end.time() == time(0, 0):
                end += timedelta(days=1)
            if start < end:
                return max(start, now), end
        except ValueError:
            pass
    return now, day_start + timedelta(days=8)


# ── Free/busy + slot computation ─────────────────────────────────────

_BUSY_LINE_RE = re.compile(r"^\s*(\S+)\s*→\s*(\S+)\s*$")


def parse_freebusy_output(text: str) -> list[tuple[datetime, datetime]] | None:
    """Parse `google__check_freebusy` output into busy intervals.

    Returns None when the output matches neither the BUSY nor the FREE
    format (auth error, API error) — callers must treat None as "unknown",
    never as "free".
    """
    busy: list[tuple[datetime, datetime]] = []
    recognized = False
    for line in str(text or "").splitlines():
        if ": FREE" in line or ": BUSY at:" in line:
            recognized = True
            continue
        match = _BUSY_LINE_RE.match(line)
        if not match:
            continue
        try:
            start = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
            end = datetime.fromisoformat(match.group(2).replace("Z", "+00:00"))
        except ValueError:
            continue
        if start.tzinfo is not None and end.tzinfo is not None and start < end:
            busy.append((start, end))
            recognized = True
    return busy if recognized else None


def compute_candidate_slots(
    busy: list[tuple[datetime, datetime]],
    window_start: datetime,
    window_end: datetime,
    *,
    duration_minutes: int,
    business_hours: tuple[time, time],
    business_days: set[int],
    buffer_minutes: int,
    now: datetime,
    max_candidates: int = MAX_CANDIDATES,
) -> list[datetime]:
    """Earliest-first free slots on the :00/:30 grid within business hours.

    A slot is free when [start - buffer, end + buffer) overlaps no busy
    interval. All datetimes must be tz-aware; day boundaries are computed in
    wall-clock time of the window's zone, so DST transitions keep 09:00 at
    09:00 local.
    """
    tz = window_start.tzinfo
    duration = timedelta(minutes=duration_minutes)
    buffer = timedelta(minutes=buffer_minutes)
    earliest = max(window_start, now + timedelta(minutes=MIN_LEAD_MINUTES))

    candidates: list[datetime] = []
    day = window_start.astimezone(tz).date()
    last_day = window_end.astimezone(tz).date()
    while day <= last_day and len(candidates) < max_candidates:
        if day.weekday() in business_days:
            slot = datetime.combine(day, business_hours[0], tzinfo=tz)
            day_end = datetime.combine(day, business_hours[1], tzinfo=tz)
            while slot + duration <= day_end and len(candidates) < max_candidates:
                slot_end = slot + duration
                if slot >= earliest and slot_end <= window_end:
                    padded_start, padded_end = slot - buffer, slot_end + buffer
                    if not any(b_start < padded_end and b_end > padded_start for b_start, b_end in busy):
                        candidates.append(slot)
                slot += timedelta(minutes=SLOT_GRID_MINUTES)
        day += timedelta(days=1)
    return candidates


def describe_slot(dt: datetime, language: str = "en") -> str:
    """Spoken/readable slot label: 'Dienstag, 25.08.2026 um 14:00 Uhr'."""
    lang = "de" if str(language).lower().startswith("de") else "en"
    weekday = _WEEKDAYS[lang][dt.weekday()]
    if lang == "de":
        return f"{weekday}, {dt.strftime('%d.%m.%Y')} um {dt.strftime('%H:%M')} Uhr"
    return f"{weekday}, {dt.strftime('%B %d, %Y')} at {dt.strftime('%H:%M')}"


# ── Call context (KNOWN FACTS + negotiation rules) ───────────────────


def build_call_context(call_sid: str, settings: Any, language: str) -> str:
    """The appointment block appended to the voice system prompt, entirely
    from the call-language pack. Empty for calls without a scheduling task."""
    task = _tasks.get(call_sid)
    if task is None or not task.candidates:
        return ""
    from pincer.voice.language import de_formality
    from pincer.voice.prompts import get_prompt

    lines = []
    for iso in task.candidates:
        dt = datetime.fromisoformat(iso)
        lines.append(f"- {iso} — {describe_slot(dt, language)}")
    template = str(get_prompt("APPOINTMENT_NEGOTIATION_RULES", language, de_formality(settings)) or "")
    return template.format(
        contact_name=task.contact_name or task.target_number,
        topic=task.topic,
        duration_minutes=task.duration_minutes,
        candidates="\n".join(lines),
    )


def process_appointment_response(
    response: str,
    state: CallState,
    settings: Any,
    transcript: TranscriptLogger | None = None,
) -> str:
    """Parse/validate the [APPOINTMENT_CONFIRMED:<ISO>] token in an agent
    response. Valid slot → recorded + token stripped. Out-of-candidates slot →
    the hard backstop: the confirmation is NOT spoken; the localized deferral
    line replaces it and the counter-proposal is recorded for the report."""
    task = _tasks.get(state.call_sid)
    if task is None:
        return response
    match = APPOINTMENT_TOKEN_RE.search(response)
    if match is None:
        return response

    from pincer.voice.language import de_formality
    from pincer.voice.prompts import get_prompt

    raw = match.group(1).strip()
    stripped = APPOINTMENT_TOKEN_RE.sub("", response).strip()
    tz = get_voice_timezone(settings)

    confirmed: str | None = None
    try:
        proposed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if proposed.tzinfo is None:
            proposed = proposed.replace(tzinfo=tz)
        for iso in task.candidates:
            if datetime.fromisoformat(iso) == proposed:
                confirmed = iso
                break
    except ValueError:
        pass

    if confirmed is not None:
        task.status = "confirmed"
        task.agreed_start = confirmed
        if transcript is not None:
            transcript.log_action(
                "appointment_confirmed",
                input_summary=task.topic,
                output_summary=confirmed,
                user_confirmed=True,
            )
        logger.info("Appointment confirmed [%s]: %s", state.call_sid, confirmed)
        return stripped or str(get_prompt("APPOINTMENT_CONFIRM_ACK", state.language, de_formality(settings)))

    # Out-of-candidates (or unparseable) confirmation: never speak it.
    task.status = "out_of_candidates"
    task.proposed_out_of_slot = raw
    if transcript is not None:
        transcript.log_action("appointment_deferred", input_summary=raw, output_summary="outside offered slots")
    logger.warning("Out-of-candidates confirmation blocked [%s]: %r", state.call_sid, raw)
    return str(get_prompt("APPOINTMENT_DEFER_LINE", state.language, de_formality(settings)))


# ── Tool entry point ─────────────────────────────────────────────────


async def schedule_appointment_call(
    tools: ToolRegistry,
    settings: Any,
    *,
    target_number: str,
    contact_name: str,
    topic: str,
    timeframe: str,
    duration_minutes: int = 30,
    language: str = "",
    attendees: str = "",
    location_or_meet: str = "",
    user_id: str = "",
    channel: str = "",
) -> str:
    """Full pre-call phase: free/busy → candidates → place the call.

    Returns a user-facing status string (tool contract, like make_phone_call:
    failures are 'Error: ...' strings, never raises)."""
    from pincer.voice.language import resolve_call_language
    from pincer.voice.localtime import voice_now

    lang = resolve_call_language(settings, language)
    report_lang = "de" if lang == "de" else "en"

    if "google__check_freebusy" not in tools.list_tools():
        return (
            "Error: Google Calendar is not connected — free slots cannot be computed. Run `pincer setup-google` first."
        )

    duration_minutes = max(5, min(int(duration_minutes or 30), 480))
    now = voice_now(settings)
    window_start, window_end = resolve_timeframe(timeframe, now)

    calendar_id = str(getattr(settings, "scheduling_calendar_id", "") or "primary")
    try:
        freebusy_text = await tools.execute(
            "google__check_freebusy",
            {
                "emails": calendar_id,
                "time_min": window_start.isoformat(),
                "time_max": window_end.isoformat(),
            },
        )
    except Exception as e:
        logger.exception("Free/busy lookup failed")
        return f"Error: could not read the calendar free/busy data: {e}"

    busy = parse_freebusy_output(freebusy_text)
    if busy is None:
        # Unknown availability must never become "offer everything as free"
        return f"Error: unrecognized free/busy response from Google Calendar: {str(freebusy_text)[:200]}"

    candidates = compute_candidate_slots(
        busy,
        window_start,
        window_end,
        duration_minutes=duration_minutes,
        business_hours=parse_business_hours(getattr(settings, "business_hours", "")),
        business_days=parse_business_days(getattr(settings, "business_days", "")),
        buffer_minutes=int(getattr(settings, "slot_buffer_min", 15) or 0),
        now=now,
    )
    if not candidates:
        if report_lang == "de":
            return (
                "Error: Im gewünschten Zeitraum ist kein freier Slot verfügbar "
                f"({window_start:%d.%m.%Y} – {window_end:%d.%m.%Y}, Geschäftszeiten). "
                "Kein Anruf wurde gestartet — bitte anderen Zeitraum wählen oder Kalender freiräumen."
            )
        return (
            "Error: no free slot available in the requested timeframe "
            f"({window_start:%Y-%m-%d} – {window_end:%Y-%m-%d}, business hours). "
            "No call was placed — pick another timeframe or free up the calendar."
        )

    task = AppointmentTask(
        task_id=uuid.uuid4().hex,
        user_id=user_id or "unknown",
        channel=channel,
        target_number=target_number,
        contact_name=contact_name,
        topic=topic,
        timeframe=timeframe,
        duration_minutes=duration_minutes,
        language=lang,
        attendees=attendees,
        location_or_meet=location_or_meet,
        candidates=[c.isoformat() for c in candidates],
    )
    result = await _place_call(task, settings)
    if result.startswith("Error"):
        return result

    slot_list = "; ".join(describe_slot(c, report_lang) for c in candidates)
    if report_lang == "de":
        return f"{result}\nTerminvorschläge für das Gespräch: {slot_list}. Ich melde mich mit dem Ergebnis."
    return f"{result}\nCandidate slots for the call: {slot_list}. I'll report back with the outcome."


async def _place_call(task: AppointmentTask, settings: Any) -> str:
    """One dial attempt through the validated make_phone_call path; registers
    the appointment context for the new call SID on success."""
    from pincer.voice.outbound import make_phone_call

    purpose = f"Schedule appointment with {task.contact_name or task.target_number}: {task.topic}"
    result = await make_phone_call(
        target_number=task.target_number,
        purpose=purpose,
        language=task.language,
        context={"user_id": task.user_id, "channel": task.channel},
    )
    if result.startswith("Error"):
        return result
    task.attempts += 1
    sid_match = re.search(r"Call SID: (\S+)", result)
    if sid_match:
        register_appointment(sid_match.group(1), task)
    else:  # pragma: no cover — make_phone_call always reports the SID on success
        logger.error("Appointment call placed but no Call SID parsed — context not attached")
    return result


# ── Retry policy (voicemail / no-answer) ─────────────────────────────


def _report_strings(lang: str) -> dict[str, str]:
    if lang == "de":
        return {
            "retrying": "📞 {reason} — neuer Versuch in {delay} Min (Versuch {next_attempt}/{total}).",
            "gave_up": (
                "📵 {contact} war nach {attempts} Versuchen nicht erreichbar ({reason}). "
                "Vorgeschlagene Slots waren: {slots}. Soll ich es später erneut versuchen?"
            ),
            "redial_failed": "⚠️ Der erneute Anruf bei {contact} konnte nicht gestartet werden: {error}",
        }
    return {
        "retrying": "📞 {reason} — retrying in {delay} min (attempt {next_attempt}/{total}).",
        "gave_up": (
            "📵 Could not reach {contact} after {attempts} attempt(s) ({reason}). "
            "Proposed slots were: {slots}. Want me to try again later?"
        ),
        "redial_failed": "⚠️ The retry call to {contact} could not be placed: {error}",
    }


async def handle_call_not_connected(call_sid: str, reason: str, settings: Any) -> bool:
    """Hook for the Twilio /status terminal branches (voicemail, no-answer,
    busy, failed). Schedules a redial or sends the final failure report.
    Returns True when the call belonged to an appointment task."""
    task = clear_appointment(call_sid)
    if task is None:
        return False

    lang = "de" if task.language == "de" else "en"
    strings = _report_strings(lang)
    contact = task.contact_name or task.target_number
    max_retries = int(getattr(settings, "voice_retry_attempts", 2) or 0)
    delay_min = int(getattr(settings, "voice_retry_delay_min", 30) or 30)

    if task.attempts <= max_retries:
        text = strings["retrying"].format(
            reason=reason, delay=delay_min, next_attempt=task.attempts + 1, total=max_retries + 1
        )
        await _notify_user(task, text)
        _schedule_redial(task, settings, delay_s=delay_min * 60.0)
        return True

    slots = "; ".join(describe_slot(datetime.fromisoformat(c), lang) for c in task.candidates)
    # Sprint 9: the retry policy is exhausted — this task is one unreachable
    # booking attempt, not N failed calls (keyed by task_id, so redials of the
    # same task never inflate the denominator).
    await _record_booking(settings, task, call_sid, BookingResult.UNREACHABLE, reason)
    await _notify_user(
        task, strings["gave_up"].format(contact=contact, attempts=task.attempts, reason=reason, slots=slots)
    )
    return True


def _schedule_redial(task: AppointmentTask, settings: Any, delay_s: float) -> None:
    async def _redial() -> None:
        try:
            await asyncio.sleep(delay_s)
            result = await _place_call(task, settings)
            if result.startswith("Error"):
                lang = "de" if task.language == "de" else "en"
                contact = task.contact_name or task.target_number
                await _notify_user(task, _report_strings(lang)["redial_failed"].format(contact=contact, error=result))
        except asyncio.CancelledError:  # shutdown
            raise
        except Exception:
            logger.exception("Appointment redial failed [task=%s]", task.task_id)

    retry = asyncio.create_task(_redial(), name=f"appointment-redial-{task.task_id}")
    _retry_tasks.add(retry)
    retry.add_done_callback(_retry_tasks.discard)


async def _record_booking(
    settings: Any,
    task: AppointmentTask,
    call_sid: str,
    result: BookingResult,
    detail: str = "",
) -> None:
    """Persist the booking SLI datapoint (Sprint 9, T9.1). Never raises."""
    from pincer.observability.bookings import record_booking_outcome

    with contextlib.suppress(Exception):
        await record_booking_outcome(
            settings,
            task_id=task.task_id,
            result=result,
            call_sid=call_sid,
            language=task.language,
            attempts=max(1, task.attempts),
            detail=detail,
        )


async def _notify_user(task: AppointmentTask, text: str) -> None:
    from pincer.voice.status_notify import send_user_message

    with contextlib.suppress(Exception):
        await send_user_message(task.user_id, task.channel, text)


# ── Post-call executor (calendar write + report note) ────────────────


async def finalize_appointment(
    tools: ToolRegistry | None,
    settings: Any,
    task: AppointmentTask,
    call_sid: str,
    language: str,
) -> str:
    """Runs from the post-call pipeline for calls that carried a scheduling
    task and actually conversed. Returns a report note (may be empty).

    confirmed → idempotent google__create_event with attendees +
    sendUpdates=all (invitations) and retry; failure is reported honestly and
    NEVER claimed as success. out_of_candidates → deferral note."""
    lang = "de" if str(language).lower().startswith("de") else "en"
    contact = task.contact_name or task.target_number

    if task.status == "confirmed" and task.agreed_start:
        slot = describe_slot(datetime.fromisoformat(task.agreed_start), lang)
        if tools is None or "google__create_event" not in tools.list_tools():
            await _record_booking(settings, task, call_sid, BookingResult.CALENDAR_FAILED, "calendar not connected")
            if lang == "de":
                return (
                    f"⚠️ Der Termin wurde am Telefon bestätigt ({slot}), aber Google Calendar ist "
                    "nicht verbunden — bitte manuell eintragen."
                )
            return (
                f"⚠️ The appointment was confirmed on the call ({slot}), but Google Calendar is "
                "not connected — please add it manually."
            )
        try:
            result = await _write_calendar_event(tools, settings, task, call_sid)
        except Exception as e:
            logger.exception("Calendar write failed after verbal confirmation [%s]", call_sid)
            await _record_booking(settings, task, call_sid, BookingResult.CALENDAR_FAILED, str(e))
            if lang == "de":
                return (
                    f"⚠️ Der Termin wurde am Telefon bestätigt ({slot}), aber der Kalendereintrag ist "
                    f"fehlgeschlagen: {e}. Der Termin steht NICHT im Kalender. "
                    "➡️ Soll ich den Eintrag erneut versuchen? Antworte einfach mit Ja."
                )
            return (
                f"⚠️ The appointment was confirmed on the call ({slot}), but writing the calendar event "
                f"failed: {e}. The event is NOT in the calendar. "
                "➡️ Shall I retry creating it? Just reply yes."
            )
        await _record_booking(settings, task, call_sid, BookingResult.CONFIRMED, task.agreed_start)
        link_match = re.search(r"Link: (\S+)", result)
        link = link_match.group(1) if link_match else ""
        task.calendar_event_link = link
        invited = task.attendees.strip()
        if lang == "de":
            note = f"📅 Termin eingetragen: {slot} mit {contact}."
            if invited:
                note += f" Einladungen gesendet an: {invited}."
            if link:
                note += f"\n🔗 {link}"
            return note
        note = f"📅 Event created: {slot} with {contact}."
        if invited:
            note += f" Invitations sent to: {invited}."
        if link:
            note += f"\n🔗 {link}"
        return note

    if task.status == "out_of_candidates":
        proposed = task.proposed_out_of_slot
        await _record_booking(settings, task, call_sid, BookingResult.OUT_OF_SLOTS, proposed)
        if lang == "de":
            return (
                f"📲 {contact} hat eine Zeit außerhalb der freien Slots vorgeschlagen ({proposed}). "
                "Ich habe nicht zugesagt — bitte prüfen und Bescheid geben, dann melde ich mich dort zurück."
            )
        return (
            f"📲 {contact} proposed a time outside the free slots ({proposed}). "
            "I did not commit — please check and let me know, and I'll confirm back with them."
        )

    # pending / no agreement: the outcome-extraction report covers the
    # conversation; add the offered slots for transparency.
    await _record_booking(settings, task, call_sid, BookingResult.DECLINED, task.status)
    slots = "; ".join(describe_slot(datetime.fromisoformat(c), lang) for c in task.candidates)
    if lang == "de":
        return f"ℹ️ Kein Termin vereinbart. Angebotene Slots: {slots}."
    return f"ℹ️ No appointment was agreed. Offered slots: {slots}."


async def _write_calendar_event(tools: ToolRegistry, settings: Any, task: AppointmentTask, call_sid: str) -> str:
    """Idempotent, retried calendar write. The idempotency key is the task id
    (stable across dial attempts), stored in the event's extended properties —
    a retry after a timeout that actually wrote never duplicates. The event
    description carries the call SID so the event links back to the call
    (/transcript <sid>)."""
    start = datetime.fromisoformat(task.agreed_start)
    end = start + timedelta(minutes=task.duration_minutes)
    tz = get_voice_timezone(settings)
    location = task.location_or_meet.strip()
    add_meet = location.lower() in _MEET_KEYWORDS

    args: dict[str, Any] = {
        "summary": task.topic if not task.contact_name else f"{task.topic} — {task.contact_name}",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "calendar_id": str(getattr(settings, "scheduling_calendar_id", "") or "primary"),
        "description": f"Scheduled by Pincer voice call {call_sid} (task {task.task_id}).",
        "timezone": str(tz),
        "attendees": task.attendees,
        "send_updates": "all",
        "idempotency_key": f"pincer-appointment-{task.task_id}",
    }
    if add_meet:
        args["add_meet_link"] = True
    elif location:
        args["location"] = location

    last_error: Exception | None = None
    for attempt, delay in enumerate((0.0, *CALENDAR_RETRY_DELAYS_S)):
        if delay:
            await asyncio.sleep(delay)
        try:
            result = await tools.execute("google__create_event", dict(args))
            if attempt:
                logger.info("Calendar write succeeded on retry %d [task=%s]", attempt, task.task_id)
            return result
        except Exception as e:
            last_error = e
            logger.warning("Calendar write attempt %d failed [task=%s]: %s", attempt + 1, task.task_id, e)
    raise last_error if last_error else RuntimeError("calendar write failed")
