"""
German comprehension helpers (Sprint 2, T2.4).

Deterministic parsing of German relative dates/times as spoken on the phone —
"morgen", "übermorgen", "nächste Woche Dienstag", "um halb drei" (= 14:30!),
"Viertel nach vier", "in acht Tagen" (idiomatically one week) — plus rendering
of dates/times into spoken German ("Dienstag, der achtzehnte August um
vierzehn Uhr dreißig"). Raw ISO strings must never reach TTS.

The deterministic parser runs first; when it returns None the caller falls
back to the LLM. Either way the resolved value is spoken back in VERIFY
before it is used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

DEFAULT_TZ = ZoneInfo("Europe/Berlin")

WEEKDAYS_DE = {
    "montag": 0,
    "dienstag": 1,
    "mittwoch": 2,
    "donnerstag": 3,
    "freitag": 4,
    "samstag": 5,
    "sonnabend": 5,
    "sonntag": 6,
}

WEEKDAY_NAMES_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]

MONTH_NAMES_DE = [
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
]

_NUMBER_WORDS = {
    "ein": 1,
    "eins": 1,
    "eine": 1,
    "einer": 1,
    "einem": 1,
    "zwei": 2,
    "drei": 3,
    "vier": 4,
    "fünf": 5,
    "sechs": 6,
    "sieben": 7,
    "acht": 8,
    "neun": 9,
    "zehn": 10,
    "elf": 11,
    "zwölf": 12,
    "dreizehn": 13,
    "vierzehn": 14,
}

_WORD_OR_DIGIT = r"(?:\d{1,2}|" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")"


def _num(token: str) -> int:
    token = token.strip().lower()
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.get(token, -1)


@lru_cache(maxsize=1)
def _minute_words() -> dict[str, int]:
    """Spoken minute forms, derived from `number_words_de` so the parser can
    always read back a time this module itself spoke.

    `render_time_de` says "vierzehn Uhr dreißig"; without the inverse mapping
    the minute is dropped and the caller hears a confident ":00" back in
    VERIFY. Deriving it keeps the two halves from drifting apart.
    """
    words = {number_words_de(n): n for n in range(60)}
    words["ein"] = 1  # spoken unit form; number_words_de(1) renders "eins"
    return words


@lru_cache(maxsize=1)
def _minute_word_or_digit() -> str:
    """Regex alternation for a minute: digits, or any spoken form.

    Longest-first so "fünfzehn"/"fünfundvierzig" win over the "fünf" prefix.
    """
    return r"(?:\d{1,2}|" + "|".join(sorted(_minute_words(), key=len, reverse=True)) + r")"


def _minute_num(token: str) -> int:
    token = token.strip().lower()
    if token.isdigit():
        return int(token)
    return _minute_words().get(token, -1)


@dataclass
class ParsedDateTimeDe:
    """Result of deterministic German date/time parsing."""

    dt: datetime
    matched: str
    has_date: bool
    has_time: bool


# "nachts" spans the turn of the night, so it is the one daytime marker that
# does not simply mean PM. Spoken as 12-hour clock: "neun/zehn/elf Uhr nachts"
# is 21:00–23:00, "zwölf Uhr nachts" is midnight, and "ein bis acht Uhr nachts"
# is the small hours and must stay AM.
_NIGHT_PM_FROM = 9


def _apply_daytime_heuristic(hour: int, text: str, *, twelve_hour_wrap: bool = False) -> int:
    """Phone calls are business-hours by default: bare small hours ("um drei",
    "halb drei") mean the afternoon unless the caller says morning/night.

    ``twelve_hour_wrap`` marks an hour produced by "half to"/"quarter to"
    arithmetic ("halb eins" → 1 - 1 = 0). There 0 is the 12-hour clock's twelve
    o'clock, not midnight — unlike an explicit "um 0 Uhr 30", which really is
    00:30 and must pass through untouched.
    """
    if twelve_hour_wrap and hour == 0:
        # "halb eins" is 12:30 (lunchtime); only a night or early-morning
        # marker makes it 00:30.
        if re.search(r"\b(nachts|nacht|morgens|früh)\b", text):
            return 0
        return 12
    if re.search(r"\b(morgens|früh|vormittag|vormittags)\b", text):
        return hour
    # Check "nachts" before the evening markers: "ein Uhr nachts" is 01:00, not
    # 13:00, and booking the opposite half of the day would survive VERIFY
    # because we would read the wrong time back as if it were what was said.
    if re.search(r"\b(nachts|nacht)\b", text):
        if hour == 12:
            return 0  # "zwölf Uhr nachts" = midnight
        if _NIGHT_PM_FROM <= hour <= 11:
            return hour + 12  # late evening end of the night
        return hour  # small hours stay AM
    if re.search(r"\b(abends|abend)\b", text) and hour < 12:
        return hour + 12
    if re.search(r"\b(nachmittags|nachmittag|mittags)\b", text) and hour < 12:
        return hour + 12
    if 1 <= hour <= 7:
        return hour + 12
    return hour


def _parse_time(text: str) -> tuple[int, int, str] | None:
    """Extract (hour, minute, matched_text) from German spoken time."""
    # "um 14:30 (Uhr)" / "um 14.30"
    m = re.search(r"\bum\s+(\d{1,2})[:.](\d{2})(?:\s*uhr)?\b", text)
    if m:
        return int(m.group(1)), int(m.group(2)), m.group(0)

    # "um 14 Uhr 30" / "um vierzehn Uhr dreißig" / "um 14 Uhr"
    m = re.search(rf"\bum\s+({_WORD_OR_DIGIT})\s+uhr(?:\s+({_minute_word_or_digit()}))?\b", text)
    if m:
        hour = _num(m.group(1))
        minute = _minute_num(m.group(2)) if m.group(2) else 0
        if hour >= 0 and 0 <= minute <= 59:
            # Naming a minute says nothing about which half of the day was
            # meant: "um drei Uhr 15" is as much the afternoon as "um drei
            # Uhr" is. Every other pattern here applies the heuristic, so
            # gating it on the minute left this branch alone reading 1–12 as
            # literal AM.
            if hour <= 12:
                hour = _apply_daytime_heuristic(hour, text)
            return hour, minute, m.group(0)

    # "halb drei" = 14:30 (half TO three, plus daytime heuristic)
    m = re.search(rf"\b(?:um\s+)?halb\s+({_WORD_OR_DIGIT})\b", text)
    if m:
        target = _num(m.group(1))
        if target > 0:
            hour = _apply_daytime_heuristic(target - 1, text, twelve_hour_wrap=True)
            return hour, 30, m.group(0)

    # "dreiviertel vier" = 15:45 (southern German)
    m = re.search(rf"\b(?:um\s+)?dreiviertel\s+({_WORD_OR_DIGIT})\b", text)
    if m:
        target = _num(m.group(1))
        if target > 0:
            hour = _apply_daytime_heuristic(target - 1, text, twelve_hour_wrap=True)
            return hour, 45, m.group(0)

    # "Viertel nach vier" = 16:15 / "Viertel vor fünf" = 16:45
    m = re.search(rf"\b(?:um\s+)?viertel\s+(nach|vor)\s+({_WORD_OR_DIGIT})\b", text)
    if m:
        base = _num(m.group(2))
        if base > 0:
            if m.group(1) == "nach":
                hour, minute = base, 15
            else:
                hour, minute = base - 1, 45
            hour = _apply_daytime_heuristic(hour, text, twelve_hour_wrap=True)
            return hour, minute, m.group(0)

    # bare "um drei"
    m = re.search(rf"\bum\s+({_WORD_OR_DIGIT})\b(?!\s*uhr)", text)
    if m:
        hour = _num(m.group(1))
        if hour >= 0:
            hour = _apply_daytime_heuristic(hour, text)
            return hour, 0, m.group(0)

    return None


def _parse_date(text: str, now: datetime) -> tuple[datetime, str] | None:
    """Extract (date, matched_text) from German relative date expressions."""
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    m = re.search(r"\bübermorgen\b", text)
    if m:
        return today + timedelta(days=2), m.group(0)

    # standalone "morgen" = tomorrow (not "morgens"/"am Morgen" = in the morning)
    m = re.search(r"\bmorgen\b(?!s)", text)
    if m and not re.search(r"\b(am|vom)\s+morgen\b", text):
        return today + timedelta(days=1), m.group(0)

    m = re.search(r"\bheute\b", text)
    if m:
        return today, m.group(0)

    # "in acht Tagen" is idiomatically one week; other "in N Tagen" are literal
    m = re.search(rf"\bin\s+({_WORD_OR_DIGIT})\s+tagen?\b", text)
    if m:
        days = _num(m.group(1))
        if days == 8:
            days = 7
        if days > 0:
            return today + timedelta(days=days), m.group(0)

    m = re.search(rf"\bin\s+({_WORD_OR_DIGIT})\s+wochen?\b", text)
    if m:
        weeks = _num(m.group(1))
        if weeks > 0:
            return today + timedelta(weeks=weeks), m.group(0)

    weekday_pattern = "|".join(WEEKDAYS_DE)

    # "nächste Woche Dienstag" / "Dienstag nächste Woche" → that weekday in the NEXT ISO week
    m = re.search(
        rf"\b(?:nächste\s+woche\s+(?:am\s+)?({weekday_pattern})|({weekday_pattern})\s+nächste\s+woche)\b",
        text,
    )
    if m:
        weekday = WEEKDAYS_DE[(m.group(1) or m.group(2))]
        start_of_next_week = today + timedelta(days=7 - today.weekday())
        return start_of_next_week + timedelta(days=weekday), m.group(0)

    # "nächsten Dienstag" / "am Dienstag" / bare weekday → next occurrence (never today)
    m = re.search(rf"\b(?:nächsten\s+|kommenden\s+|am\s+)?({weekday_pattern})\b", text)
    if m:
        weekday = WEEKDAYS_DE[m.group(1)]
        days_ahead = (weekday - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        return today + timedelta(days=days_ahead), m.group(0)

    return None


def parse_relative_datetime_de(
    text: str,
    now: datetime | None = None,
    tz: ZoneInfo | None = None,
) -> ParsedDateTimeDe | None:
    """Parse a German relative date/time expression deterministically.

    Returns None when nothing matched — the caller then falls back to the LLM.
    The resolved value must always be echoed back (render_datetime_de) in
    VERIFY before it is acted on.
    """
    tz = tz or DEFAULT_TZ
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    lowered = " ".join(str(text or "").lower().split())
    if not lowered:
        return None

    date_result = _parse_date(lowered, now)
    time_result = _parse_time(lowered)
    if date_result is None and time_result is None:
        return None

    matched_parts = []
    if date_result is not None:
        base_date, date_match = date_result
        matched_parts.append(date_match)
    else:
        base_date = now.replace(hour=0, minute=0, second=0, microsecond=0)

    hour, minute = 9, 0
    if time_result is not None:
        hour, minute, time_match = time_result
        matched_parts.append(time_match)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None

    dt = base_date.replace(hour=hour, minute=minute)

    # Time-only expressions mean the next occurrence of that clock time
    if date_result is None and time_result is not None and dt <= now:
        dt += timedelta(days=1)

    return ParsedDateTimeDe(
        dt=dt,
        matched=" ".join(matched_parts),
        has_date=date_result is not None,
        has_time=time_result is not None,
    )


# ── Spoken German rendering (numbers, dates, times) ──────────────────────────

_UNITS_DE = ["null", "eins", "zwei", "drei", "vier", "fünf", "sechs", "sieben", "acht", "neun"]
_TEENS_DE = [
    "zehn",
    "elf",
    "zwölf",
    "dreizehn",
    "vierzehn",
    "fünfzehn",
    "sechzehn",
    "siebzehn",
    "achtzehn",
    "neunzehn",
]
_TENS_DE = {20: "zwanzig", 30: "dreißig", 40: "vierzig", 50: "fünfzig"}


def number_words_de(n: int) -> str:
    """German number words for 0-59 (enough for clock times and day ordinals)."""
    if not 0 <= n <= 59:
        return str(n)
    if n < 10:
        return _UNITS_DE[n]
    if n < 20:
        return _TEENS_DE[n - 10]
    tens, unit = divmod(n, 10)
    tens_word = _TENS_DE[tens * 10]
    if unit == 0:
        return tens_word
    unit_word = "ein" if unit == 1 else _UNITS_DE[unit]
    return f"{unit_word}und{tens_word}"


def ordinal_words_de(day: int) -> str:
    """German ordinal (nominative, 'der …') for day-of-month 1-31."""
    irregular = {1: "erste", 3: "dritte", 7: "siebte", 8: "achte"}
    if day in irregular:
        return irregular[day]
    if 1 <= day <= 19:
        return f"{number_words_de(day)}te"
    if 20 <= day <= 31:
        return f"{number_words_de(day)}ste"
    return str(day)


def render_time_de(dt: datetime) -> str:
    """'vierzehn Uhr dreißig' / 'vierzehn Uhr' — no digits, no ISO."""
    hour_word = number_words_de(dt.hour) if dt.hour != 1 else "ein"
    if dt.minute == 0:
        return f"{hour_word} Uhr"
    return f"{hour_word} Uhr {number_words_de(dt.minute)}"


def render_date_de(dt: datetime) -> str:
    """'Dienstag, der achtzehnte August'."""
    weekday = WEEKDAY_NAMES_DE[dt.weekday()]
    return f"{weekday}, der {ordinal_words_de(dt.day)} {MONTH_NAMES_DE[dt.month - 1]}"


def render_datetime_de(dt: datetime, include_time: bool = True) -> str:
    """'Dienstag, der achtzehnte August um vierzehn Uhr dreißig'."""
    date_part = render_date_de(dt)
    if not include_time:
        return date_part
    return f"{date_part} um {render_time_de(dt)}"
