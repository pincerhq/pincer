"""
Speech rendering of in-call tool results (Sprint 11, §7).

Raw tool output MUST NOT reach TTS — and, since the LLM phrases the reply from
what it is told, it must not reach the LLM as raw data either. ``render``
turns a tool's text result into one deterministic, speakable sentence per
language pack (datetimes via the Sprint-2 spoken renderer), and
``describe_action`` renders the exact commitment spoken before a write.

The last line of defence is ``ensure_speakable``: whatever template or parser
produced, a rendered string never contains braces or ISO timestamps. If it
would, the generic fallback is spoken instead.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any

from pincer.voice.prompts import get_prompt

logger = logging.getLogger(__name__)

# Anything that looks like machine data: JSON braces and ISO-8601 timestamps.
ISO_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
RAW_DATA_RE = re.compile(r"[{}]|\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")

# Tool results whose first line starts like this are failures.
_ERROR_PREFIXES = ("error", "[error", "failed", "exception")

_WEEKDAYS = {
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "uk": ["понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя"],
}
_MONTHS = {
    "en": [
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ],
    "uk": [
        "січня",
        "лютого",
        "березня",
        "квітня",
        "травня",
        "червня",
        "липня",
        "серпня",
        "вересня",
        "жовтня",
        "листопада",
        "грудня",
    ],
}

MAX_LIST_ITEMS = 3


def _lang(language: str) -> str:
    return str(language or "en").strip().lower()[:2] or "en"


def is_speakable(text: str) -> bool:
    """True when the text carries no JSON braces and no ISO timestamps."""
    return not RAW_DATA_RE.search(text or "")


def ensure_speakable(text: str, language: str, fallback_key: str = "TOOL_ERROR") -> str:
    """Final guard: never let raw data through to the TTS path."""
    if text and is_speakable(text):
        return text
    logger.warning("Unspeakable tool rendering suppressed: %r", (text or "")[:120])
    return str(get_prompt(fallback_key, language) or "")


# ── Datetime rendering (Sprint-2 renderer for de; spoken forms otherwise) ──


def parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def spoken_datetime(dt: datetime, language: str, include_time: bool = True) -> str:
    """'Dienstag, der achtzehnte August um vierzehn Uhr' / 'Tuesday, August 18 at 2:00 PM'."""
    lang = _lang(language)
    if lang == "de":
        from pincer.voice.nlu_de import render_datetime_de

        return render_datetime_de(dt, include_time=include_time)
    if lang == "uk":
        text = f"{_WEEKDAYS['uk'][dt.weekday()]}, {dt.day} {_MONTHS['uk'][dt.month - 1]}"
        return f"{text} о {dt.strftime('%H:%M')}" if include_time else text
    text = f"{_WEEKDAYS['en'][dt.weekday()]}, {_MONTHS['en'][dt.month - 1]} {dt.day}"
    if not include_time:
        return text
    hour12 = dt.hour % 12 or 12
    suffix = "AM" if dt.hour < 12 else "PM"
    return f"{text} at {hour12}:{dt.minute:02d} {suffix}"


def spoken_time(dt: datetime, language: str) -> str:
    lang = _lang(language)
    if lang == "de":
        from pincer.voice.nlu_de import render_time_de

        return render_time_de(dt)
    if lang == "uk":
        return dt.strftime("%H:%M")
    hour12 = dt.hour % 12 or 12
    return f"{hour12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def spoken_range(start: datetime, end: datetime, language: str) -> str:
    """Same-day ranges collapse to 'day, from A to B'."""
    lang = _lang(language)
    joiner = {"de": " bis ", "uk": " до ", "en": " to "}.get(lang, " to ")
    if start.date() == end.date():
        day = spoken_datetime(start, language, include_time=False)
        prefix = {"de": " von ", "uk": " з ", "en": " from "}.get(lang, " from ")
        return f"{day}{prefix}{spoken_time(start, language)}{joiner}{spoken_time(end, language)}"
    return f"{spoken_datetime(start, language)}{joiner}{spoken_datetime(end, language)}"


def _join(items: list[str], language: str) -> str:
    lang = _lang(language)
    word = {"de": " und ", "uk": " та ", "en": " and "}.get(lang, " and ")
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + word + items[-1]


def _tpl(key: str, language: str) -> str:
    table = get_prompt("TOOL_SPEECH", language) or {}
    value = table.get(key)
    if value is None:
        value = (get_prompt("TOOL_SPEECH", "en") or {}).get(key, "")
    return str(value or "")


def _is_error(result: str) -> bool:
    first = (result or "").strip().lower()
    return not first or any(first.startswith(p) for p in _ERROR_PREFIXES)


# ── Per-tool renderers ───────────────────────────────────────────────


def _render_freebusy(result: str, args: dict[str, Any], language: str) -> str:
    from pincer.voice.scheduling import parse_freebusy_output

    busy = parse_freebusy_output(result)
    if busy is None:
        return str(get_prompt("TOOL_ERROR", language) or "")
    window_start = parse_iso(args.get("time_min"))
    window_end = parse_iso(args.get("time_max"))
    if not busy:
        return _tpl("google__check_freebusy.all_free", language)
    if window_start is None or window_end is None:
        # Without a window we can only describe the busy side honestly.
        spoken = [spoken_range(s, e, language) for s, e in sorted(busy)[:MAX_LIST_ITEMS]]
        lang = _lang(language)
        lead = {"de": "Belegt ist: ", "uk": "Зайнято: ", "en": "Busy is: "}.get(lang, "Busy is: ")
        return lead + _join(spoken, language) + "."
    # Free gaps = complement of the busy intervals inside the window.
    busy_sorted = sorted(busy)
    tz = window_start.tzinfo
    cursor = window_start
    gaps: list[tuple[datetime, datetime]] = []
    for start, end in busy_sorted:
        start = start.astimezone(tz) if tz and start.tzinfo else start
        end = end.astimezone(tz) if tz and end.tzinfo else end
        if start > cursor:
            gaps.append((cursor, min(start, window_end)))
        cursor = max(cursor, end)
        if cursor >= window_end:
            break
    if cursor < window_end:
        gaps.append((cursor, window_end))
    gaps = [(s, e) for s, e in gaps if e - s >= timedelta(minutes=15)]
    if not gaps:
        return _tpl("google__check_freebusy.none_free", language)
    slots = _join([spoken_range(s, e, language) for s, e in gaps[:MAX_LIST_ITEMS]], language)
    return _tpl("google__check_freebusy.free", language).format(slots=slots)


_EVENT_LINE_RE = re.compile(r"^\s*(\S+)\s+→\s+(\S+)\s+—\s+(.*?)(?:\s+\|.*)?$")


def _render_list_events(result: str, language: str) -> str:
    if result.strip().lower().startswith("no events"):
        return _tpl("google__list_events.none", language)
    events: list[str] = []
    for line in result.splitlines():
        match = _EVENT_LINE_RE.match(line)
        if not match:
            continue
        start = parse_iso(match.group(1))
        title = match.group(3).strip()
        if start is not None:
            events.append(f"{spoken_datetime(start, language)}: {title}" if title else spoken_datetime(start, language))
        elif title:
            events.append(title)
    if not events:
        return _tpl("google__list_events.none", language)
    shown = _join(events[:MAX_LIST_ITEMS], language)
    return _tpl("google__list_events.some", language).format(count=len(events), events=shown)


def _render_create_event(result: str, language: str) -> str:
    if "already exists" in result.lower():
        return _tpl("google__create_event.exists", language)
    return _tpl("google__create_event.ok", language)


def _render_contacts(result: str, language: str) -> str:
    names: list[str] = []
    try:
        data = json.loads(result)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, list):
        for item in data[:MAX_LIST_ITEMS]:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                if name:
                    names.append(name)
    elif isinstance(data, dict):
        name = str(data.get("name") or "").strip()
        if name:
            names.append(name)
    elif result.strip() and not result.strip().lower().startswith("no "):
        names = [line.split("|")[0].strip() for line in result.splitlines() if line.strip()][:MAX_LIST_ITEMS]
    if not names:
        return _tpl("contact_lookup.none", language)
    return _tpl("contact_lookup.some", language).format(contacts=_join(names, language))


def _render_memory_search(result: str, language: str) -> str:
    lines = [ln.strip(" -•\t") for ln in result.splitlines() if ln.strip()]
    lines = [ln for ln in lines if not ln.lower().startswith(("no ", "nothing", "keine", "нічого"))]
    if not lines:
        return _tpl("memory_search.none", language)
    cleaned = [ISO_TIMESTAMP_RE.sub("", ln).strip() for ln in lines[:MAX_LIST_ITEMS]]
    return _tpl("memory_search.some", language).format(items=_join(cleaned, language))


def _render_profile(result: str, language: str) -> str:
    text = " ".join(ln.strip() for ln in result.splitlines() if ln.strip())[:300]
    text = ISO_TIMESTAMP_RE.sub("", text).replace("{", "").replace("}", "")
    return _tpl("business_profile_lookup.ok", language).format(profile=text.rstrip("."))


def render(tool_name: str, result: str, language: str, args: dict[str, Any] | None = None) -> str:
    """Deterministic, speakable rendering of a tool result (§7)."""
    args = args or {}
    result = str(result or "")
    try:
        if _is_error(result):
            text = str(get_prompt("TOOL_ERROR", language) or "")
        elif tool_name == "google__check_freebusy":
            text = _render_freebusy(result, args, language)
        elif tool_name == "google__list_events":
            text = _render_list_events(result, language)
        elif tool_name == "google__create_event":
            text = _render_create_event(result, language)
        elif tool_name == "google__update_event":
            text = _tpl("google__update_event.ok", language)
        elif tool_name == "send_owner_message":
            text = _tpl("send_owner_message.ok", language)
        elif tool_name == "memory_note":
            text = _tpl("memory_note.ok", language)
        elif tool_name == "contact_lookup":
            text = _render_contacts(result, language)
        elif tool_name == "memory_search":
            text = _render_memory_search(result, language)
        elif tool_name == "business_profile_lookup":
            text = _render_profile(result, language)
        else:
            text = _tpl("default.ok", language)
    except Exception:
        logger.exception("Tool speech rendering failed for %s", tool_name)
        text = str(get_prompt("TOOL_ERROR", language) or "")
    return ensure_speakable(text, language)


def render_pending(action_description: str, language: str) -> str:
    return ensure_speakable(_tpl("pending", language).format(action=action_description), language)


def render_denied(language: str) -> str:
    return ensure_speakable(_tpl("denied", language), language)


# ── Action description for VERIFY_ACTION ─────────────────────────────


def describe_action(tool_name: str, args: dict[str, Any] | None, language: str) -> str:
    """The exact commitment spoken before a write: 'den Termin Beratung am
    Dienstag, der achtzehnte August um vierzehn Uhr eintragen'."""
    args = args or {}
    table = get_prompt("ACTION_DESCRIPTIONS", language) or {}
    template = str(table.get(tool_name) or table.get("default") or "{tool}")
    start = parse_iso(args.get("start"))
    when = spoken_datetime(start, language) if start else _spoken_fallback_when(args, language)
    title = str(args.get("summary") or args.get("title") or "").strip()
    text = str(args.get("text") or args.get("message") or "").strip()
    note = str(args.get("note") or args.get("content") or text).strip()
    try:
        rendered = template.format(
            tool=tool_name.replace("__", " ").replace("_", " "),
            when=when or "",
            title=f"„{title}“" if title and _lang(language) == "de" else (f"'{title}'" if title else ""),
            text=text[:160],
            note=note[:160],
        )
    except (KeyError, IndexError, ValueError):
        rendered = tool_name.replace("__", " ").replace("_", " ")
    rendered = re.sub(r"\s{2,}", " ", rendered).strip()
    return ensure_speakable(rendered, language, fallback_key="TOOL_ERROR")


def _spoken_fallback_when(args: dict[str, Any], language: str) -> str:
    for key in ("when", "date", "time", "datetime"):
        dt = parse_iso(args.get(key))
        if dt is not None:
            return spoken_datetime(dt, language)
    return ""


__all__ = [
    "ISO_TIMESTAMP_RE",
    "RAW_DATA_RE",
    "describe_action",
    "ensure_speakable",
    "is_speakable",
    "parse_iso",
    "render",
    "render_denied",
    "render_pending",
    "spoken_datetime",
    "spoken_range",
    "spoken_time",
]
