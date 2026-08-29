"""
Owner report + persistence for receptionist calls (Sprint 12 §11/§12).

The message a caller left (or the booking they made) reaches the business
owner on their own channel ≤ 30 s after hangup, in the owner's language, and
is persisted in ``inbound_messages`` with ``delivered_to_owner_at`` stamped on
success. Delivery failure retries ×3 and then raises a Sprint 9 alert — a lost
message is a lost customer.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import aiosqlite

from pincer.voice.receptionist.intents import INTENT_AFTER_HOURS, INTENT_APPOINTMENT, INTENT_MESSAGE

if TYPE_CHECKING:
    from pincer.voice.receptionist.profile import BusinessProfile

logger = logging.getLogger(__name__)

DELIVERY_RETRIES = 3
DELIVERY_RETRY_DELAY_S = 2.0


def _fmt_time(ts: datetime | None, profile: BusinessProfile | None, language: str) -> str:
    ts = ts or datetime.now(UTC)
    if profile is not None:
        ts = ts.astimezone(profile.tz)
    if language.startswith("de"):
        return ts.strftime("%d.%m.%Y %H:%M")
    return ts.strftime("%Y-%m-%d %H:%M")


def render_owner_report(
    reception: dict[str, Any],
    *,
    call_sid: str,
    profile: BusinessProfile | None,
    language: str = "de",
    ended_at: datetime | None = None,
    abusive: bool = False,
    caller_number: str = "",
) -> str:
    """§12 template, exact shape; the booking line is appended for bookings."""
    lang = str(language or "de")[:2]
    slots = dict(reception.get("slots") or {})
    name = str(slots.get("caller_name") or "")
    number = str(slots.get("callback_number") or caller_number or "")
    matter = str(slots.get("matter") or "")
    urgent = bool(slots.get("urgent"))
    unverified = bool(slots.get("caller_name_unverified"))
    business = profile.business.name if profile is not None else "Pincer"
    when = _fmt_time(ended_at, profile, lang)
    intent = str(reception.get("intent") or "")

    if lang == "de":
        unknown = "unbekannt"
        lines = [
            f"📞 Anruf für {business} — {when}",
            f"Von: {name or unknown}{' (unbestätigt)' if unverified and name else ''} · {number or unknown}",
            f"Anliegen: {matter or ('Terminbuchung' if intent == INTENT_APPOINTMENT else '—')}",
        ]
        if urgent:
            lines.append("❗ DRINGEND")
        if intent == INTENT_AFTER_HOURS:
            lines.append("🌙 Außerhalb der Öffnungszeiten angerufen")
        booking = reception.get("booking") or {}
        if booking.get("booked"):
            link = booking.get("calendar_link") or ""
            lines.append(f"📅 Termin gebucht: {booking.get('slot_spoken', '')}{' — ' + link if link else ''}")
        if abusive:
            lines.append(
                "⚠️ Der Anruf wirkte beleidigend/missbräuchlich — soll die Nummer auf die Sperrliste? "
                "Antworte mit Ja, dann lege ich sie per Freigabe an."
            )
        lines.append(f"📄 Transkript: /transcript {call_sid}")
    elif lang == "uk":
        unknown = "невідомо"
        lines = [
            f"📞 Дзвінок для {business} — {when}",
            f"Від: {name or unknown}{' (не підтверджено)' if unverified and name else ''} · {number or unknown}",
            f"Питання: {matter or ('Запис на прийом' if intent == INTENT_APPOINTMENT else '—')}",
        ]
        if urgent:
            lines.append("❗ ТЕРМІНОВО")
        if intent == INTENT_AFTER_HOURS:
            lines.append("🌙 Дзвінок поза робочими годинами")
        booking = reception.get("booking") or {}
        if booking.get("booked"):
            link = booking.get("calendar_link") or ""
            lines.append(f"📅 Зустріч заброньовано: {booking.get('slot_spoken', '')}{' — ' + link if link else ''}")
        if abusive:
            lines.append("⚠️ Дзвінок виглядав образливим — додати номер до списку блокування? Відповідайте «так».")
        lines.append(f"📄 Транскрипт: /transcript {call_sid}")
    else:
        unknown = "unknown"
        lines = [
            f"📞 Call for {business} — {when}",
            f"From: {name or unknown}{' (unverified)' if unverified and name else ''} · {number or unknown}",
            f"Matter: {matter or ('Appointment booking' if intent == INTENT_APPOINTMENT else '—')}",
        ]
        if urgent:
            lines.append("❗ URGENT")
        if intent == INTENT_AFTER_HOURS:
            lines.append("🌙 Called outside opening hours")
        booking = reception.get("booking") or {}
        if booking.get("booked"):
            link = booking.get("calendar_link") or ""
            lines.append(f"📅 Appointment booked: {booking.get('slot_spoken', '')}{' — ' + link if link else ''}")
        if abusive:
            lines.append(
                "⚠️ The call seemed abusive — add the number to the blocklist? Reply yes and I'll add it via approval."
            )
        lines.append(f"📄 Transcript: /transcript {call_sid}")
    return "\n".join(lines)


def should_report(reception: dict[str, Any]) -> bool:
    """Message/booking/after-hours calls always report; a pure FAQ call that
    left nothing behind does not wake the owner."""
    intent = str(reception.get("intent") or "")
    slots = dict(reception.get("slots") or {})
    if intent in (INTENT_MESSAGE, INTENT_APPOINTMENT, INTENT_AFTER_HOURS):
        return True
    return bool(slots.get("matter") or slots.get("caller_name") or (reception.get("booking") or {}).get("booked"))


async def persist_inbound_message(
    db_path: str,
    call_sid: str,
    reception: dict[str, Any],
    *,
    delivered_at: str | None = None,
) -> int | None:
    """Insert the inbound_messages row (idempotent per call). Returns the row id."""
    if not db_path:
        return None
    slots = dict(reception.get("slots") or {})
    try:
        from pincer.voice.retention import ensure_voice_tables

        async with aiosqlite.connect(db_path) as db:
            await ensure_voice_tables(db)
            await db.execute("DELETE FROM inbound_messages WHERE call_sid = ?", (call_sid,))
            cursor = await db.execute(
                "INSERT INTO inbound_messages (call_sid, caller_name, caller_name_unverified, callback_number, "
                "callback_unverified, matter, urgent, created_at, delivered_to_owner_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    call_sid,
                    str(slots.get("caller_name") or ""),
                    int(bool(slots.get("caller_name_unverified"))),
                    str(slots.get("callback_number") or ""),
                    int(bool(slots.get("callback_unverified"))),
                    str(slots.get("matter") or ""),
                    int(bool(slots.get("urgent"))),
                    datetime.now(UTC).isoformat(),
                    delivered_at,
                ),
            )
            intent = str(reception.get("intent") or "")
            if intent:
                with contextlib.suppress(Exception):
                    await db.execute("UPDATE voice_calls SET inbound_intent = ? WHERE call_sid = ?", (intent, call_sid))
            await db.commit()
            return int(cursor.lastrowid or 0)
    except Exception:
        logger.exception("inbound_messages persist failed [%s]", call_sid)
        return None


async def stamp_delivered(db_path: str, call_sid: str) -> None:
    if not db_path:
        return
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "UPDATE inbound_messages SET delivered_to_owner_at = ? WHERE call_sid = ?",
                (datetime.now(UTC).isoformat(), call_sid),
            )
            await db.commit()
    except Exception:
        logger.debug("delivered_to_owner_at stamp failed [%s]", call_sid, exc_info=True)


async def deliver_owner_report(
    settings: Any,
    call_sid: str,
    text: str,
    *,
    owner_user_id: str = "",
    retries: int = DELIVERY_RETRIES,
    retry_delay_s: float = DELIVERY_RETRY_DELAY_S,
) -> bool:
    """Send the report to the owner (status_notify notifier); retry ×3, then alert."""
    from pincer.voice import status_notify

    user_id = owner_user_id or str(getattr(settings, "default_user_id", "") or "")
    if not user_id:
        logger.error("Receptionist report for %s has no owner (PINCER_DEFAULT_USER_ID unset)", call_sid)
        await _alert_lost_message(settings, call_sid, "no owner user configured")
        return False
    for attempt in range(1, retries + 1):
        try:
            if await status_notify.send_user_message(user_id, "", text):
                return True
        except Exception:
            logger.exception("Owner report delivery attempt %d failed [%s]", attempt, call_sid)
        if attempt < retries:
            await asyncio.sleep(retry_delay_s)
    await _alert_lost_message(settings, call_sid, f"delivery failed after {retries} attempts")
    return False


async def _alert_lost_message(settings: Any, call_sid: str, detail: str) -> None:
    try:
        from pincer.observability.alerts import Alert, Severity, deliver

        await deliver(
            settings,
            [
                Alert(
                    rule="inbound_message_undelivered",
                    severity=Severity.PAGE,
                    title=f"Receptionist message for call {call_sid} not delivered",
                    detail=f"{detail} — the message is in inbound_messages (dashboard /api/voice/messages)",
                    value=1.0,
                    threshold=0.0,
                    runbook="docs/operations/runbook.md#inbound-message-undelivered",
                    context={"call_sid": call_sid},
                )
            ],
        )
    except Exception:
        logger.exception("Lost-message alert failed [%s]", call_sid)


__all__ = [
    "DELIVERY_RETRIES",
    "deliver_owner_report",
    "persist_inbound_message",
    "render_owner_report",
    "should_report",
    "stamp_delivered",
]
