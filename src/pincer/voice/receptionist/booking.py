"""
INBOUND_BOOKING helpers (Sprint 12 §8.3) — candidates from profile hours ∩
calendar free/busy, caller choice / counter-proposal parsing, slot re-check.

Same grid, buffer, and "earliest first" rules as Sprint 6
(`voice/scheduling.py`), generalized to the profile's per-weekday ranges.
Everything is pure; the session supplies free/busy and time.
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING

from pincer.voice.scheduling import MIN_LEAD_MINUTES, SLOT_GRID_MINUTES

if TYPE_CHECKING:
    from pincer.voice.receptionist.profile import BusinessProfile

MAX_INBOUND_CANDIDATES = 3

_WEEKDAY_WORDS = {
    "de": ["montag", "dienstag", "mittwoch", "donnerstag", "freitag", "samstag", "sonntag"],
    "en": ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"],
    "uk": ["понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя"],
}
_ORDINAL_WORDS = {
    0: ("erste", "ersten", "erster", "first", "перший", "перша", "1"),
    1: ("zweite", "zweiten", "second", "другий", "друга", "2"),
    2: ("dritte", "dritten", "third", "третій", "третя", "3"),
}
_LAST_WORDS = ("letzte", "letzten", "last", "останній", "остання")
_THIS_WEEK = ("diese woche", "this week", "цього тижня", "diese", "this")
_NEXT_WEEK = ("nächste woche", "naechste woche", "next week", "наступного тижня", "nächste", "next")
_TOMORROW = ("morgen", "tomorrow", "завтра")


def earliest_bookable(now: datetime) -> datetime:
    """The soonest a slot may start.

    One definition for both paths that decide bookability: the candidates we
    offer, and a time the CALLER proposes. A caller-proposed slot is still a
    booking, so "kann ich in fünf Minuten vorbeikommen?" has to clear the same
    bar as anything we would have offered — otherwise the lead-time guarantee
    holds everywhere except where a caller asks for it to not.
    """
    return now + timedelta(minutes=MIN_LEAD_MINUTES)


def meets_lead_time(slot: datetime, now: datetime) -> bool:
    return slot >= earliest_bookable(now)


def compute_profile_candidates(
    busy: list[tuple[datetime, datetime]],
    window_start: datetime,
    window_end: datetime,
    profile: BusinessProfile,
    *,
    duration_minutes: int,
    buffer_minutes: int,
    now: datetime,
    max_candidates: int = MAX_INBOUND_CANDIDATES,
) -> list[datetime]:
    """Earliest-first free slots inside the profile's opening ranges."""
    tz = profile.tz
    duration = timedelta(minutes=duration_minutes)
    buffer = timedelta(minutes=buffer_minutes)
    earliest = max(window_start, earliest_bookable(now))
    candidates: list[datetime] = []
    day = window_start.astimezone(tz).date()
    last_day = window_end.astimezone(tz).date()
    while day <= last_day and len(candidates) < max_candidates:
        for start_t, end_t in profile.hours_for(day.weekday()):
            slot = datetime.combine(day, start_t, tzinfo=tz)
            range_end = datetime.combine(day, end_t, tzinfo=tz)
            while slot + duration <= range_end and len(candidates) < max_candidates:
                if slot >= earliest and slot + duration <= window_end and is_slot_free(slot, duration, busy, buffer):
                    candidates.append(slot)
                slot += timedelta(minutes=SLOT_GRID_MINUTES)
        day += timedelta(days=1)
    return candidates


def is_slot_free(
    slot: datetime,
    duration: timedelta,
    busy: list[tuple[datetime, datetime]],
    buffer: timedelta = timedelta(0),
) -> bool:
    padded_start, padded_end = slot - buffer, slot + duration + buffer
    return not any(b_start < padded_end and b_end > padded_start for b_start, b_end in busy)


_MINUTES_PER_DAY = 24 * 60


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def within_hours(slot: datetime, duration: timedelta, profile: BusinessProfile) -> bool:
    """Does the whole slot, start to end, fall inside one opening range?"""
    local = slot.astimezone(profile.tz)
    end_local = (slot + duration).astimezone(profile.tz)
    start_min, end_min = _minutes(local.time()), _minutes(end_local.time())

    if end_local.date() != local.date():
        # A slot ending exactly at midnight still belongs to the start day —
        # but its end is 24:00, not 00:00. Comparing the raw 00:00 against a
        # closing time let EVERY such slot through, because 00:00 is the
        # smallest time there is: 23:30 + 30min passed a 23:45 close.
        if end_min != 0 or end_local.date() != local.date() + timedelta(days=1):
            return False
        end_min = _MINUTES_PER_DAY

    return any(
        _minutes(start_t) <= start_min and end_min <= _minutes(end_t)
        for start_t, end_t in profile.hours_for(local.weekday())
    )


def resolve_booking_window(text: str, now: datetime) -> tuple[datetime, datetime]:
    """'eher diese oder nächste Woche?' answer → (window_start, window_end)."""
    lowered = str(text or "").lower()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if any(k in lowered for k in _TOMORROW):
        start = day_start + timedelta(days=1)
        return start, start + timedelta(days=1)
    if any(k in lowered for k in _NEXT_WEEK):
        next_monday = day_start + timedelta(days=7 - day_start.weekday())
        return next_monday, next_monday + timedelta(days=7)
    if any(k in lowered for k in _THIS_WEEK):
        return now, day_start + timedelta(days=7 - day_start.weekday())
    return now, day_start + timedelta(days=8)


# A digit is only an ordinal ("the 2nd one") when it is not part of a clock
# time. "geht 3 Uhr?" is a question about three o'clock, not a pick of the
# third offered slot — and silently treating it as a pick books a slot the
# caller never named.
_EXPLICIT_CLOCK_RE = re.compile(r"\b\d{1,2}(?::\d{2}|\s*(?:uhr|o'?clock|am|pm|год))")


def _clock_spans(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in _EXPLICIT_CLOCK_RE.finditer(text)]


def _mentions_ordinal(text: str, word: str, clock_spans: list[tuple[int, int]]) -> bool:
    """Whether `word` appears as an ordinal, ignoring clock-time occurrences."""
    for m in re.finditer(rf"\b{re.escape(word)}\b", text):
        if not any(lo <= m.start() < hi for lo, hi in clock_spans):
            return True
    return False


def parse_slot_choice(text: str, candidates: list[datetime], language: str = "de") -> datetime | None:
    """Which offered candidate did the caller pick? Ordinals ("the second"),
    weekday names, or a clock time that matches exactly one candidate."""
    lowered = str(text or "").lower()
    if not candidates:
        return None
    clock_spans = _clock_spans(lowered)
    for idx, words in _ORDINAL_WORDS.items():
        if idx < len(candidates) and any(_mentions_ordinal(lowered, w, clock_spans) for w in words):
            return candidates[idx]
    if any(w in lowered for w in _LAST_WORDS):
        return candidates[-1]
    lang = str(language or "de")[:2]
    day_words = _WEEKDAY_WORDS.get(lang, _WEEKDAY_WORDS["en"])
    matches = [c for c in candidates if day_words[c.weekday()] in lowered]
    hour_matches: list[datetime] = []
    for m in re.finditer(r"\b(\d{1,2})(?::(\d{2}))?\s*(?:uhr|o'clock|am|pm|год)?", lowered):
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        if "pm" in m.group(0) and hour < 12:
            hour += 12
        hour_matches.extend(c for c in candidates if c.hour == hour and c.minute == minute)
    if len(matches) == 1 and not hour_matches:
        return matches[0]
    both = [c for c in matches if c in hour_matches] if matches else hour_matches
    if len(both) == 1:
        return both[0]
    if len(hour_matches) == 1 and not matches:
        return hour_matches[0]
    return None


def parse_counter_proposal(text: str, now: datetime, language: str = "de") -> datetime | None:
    """A time the caller proposes that is not one of the candidates."""
    lang = str(language or "de")[:2]
    if lang == "de":
        from pincer.voice.nlu_de import parse_relative_datetime_de

        parsed = parse_relative_datetime_de(text, now=now, tz=now.tzinfo)  # type: ignore[arg-type]
        if parsed is not None and parsed.has_time:
            return parsed.dt
        return None
    lowered = str(text or "").lower()
    day_words = _WEEKDAY_WORDS.get(lang, _WEEKDAY_WORDS["en"])
    target_day = None
    for idx, word in enumerate(day_words):
        if word in lowered:
            target_day = idx
            break
    if "tomorrow" in lowered or "завтра" in lowered:
        base = now + timedelta(days=1)
    elif target_day is not None:
        delta = (target_day - now.weekday()) % 7 or 7
        base = now + timedelta(days=delta)
    else:
        base = now
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|o'clock|год)?", lowered)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    if m.group(3) == "pm" and hour < 12:
        hour += 12
    if hour > 23:
        return None
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)


__all__ = [
    "MAX_INBOUND_CANDIDATES",
    "compute_profile_candidates",
    "earliest_bookable",
    "is_slot_free",
    "meets_lead_time",
    "parse_counter_proposal",
    "parse_slot_choice",
    "resolve_booking_window",
    "within_hours",
]
