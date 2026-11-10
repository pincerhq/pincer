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

import html
import json
import logging
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from pincer.integrations.google.calendar_tz import is_date_only, localize_bound
from pincer.voice.prompts import get_prompt

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Anything that looks like machine data: JSON braces and ISO-8601 timestamps.
ISO_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
RAW_DATA_RE = re.compile(r"[{}]|\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
_RANGE_JOINERS = {"de": " bis ", "uk": " до ", "en": " to "}

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
# Web search results are read back from snippets: cap what reaches the model.
# Two results leave each snippet ~140 characters, enough for the figure the
# caller asked about.
WEBSEARCH_MAX_CHARS = 400
WEBSEARCH_MAX_RESULTS = 2
_SECURITY_NOTE = "[SECURITY NOTE"

_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_MARKUP_RE = re.compile(r"<[^>]*>|[*_`#|\[\]{}]+")


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
    joiner = _RANGE_JOINERS.get(lang, " to ")
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


def _freebusy_window(args: dict[str, Any], zone: ZoneInfo | None) -> tuple[datetime | None, datetime | None]:
    """The free/busy window as the tool read it (tools_calendar._range, through the same
    calendar_tz helpers): on a call (``zone``), offset-less times are in the call's zone, a bare
    date is that whole local day, and a bare-date ``time_min`` alone is that one day. Without a
    zone, as given."""
    time_min, time_max = args.get("time_min"), args.get("time_max")
    if zone is None:
        return parse_iso(time_min), parse_iso(time_max)

    def _bound(value: Any, *, end: bool = False) -> datetime | None:
        if not isinstance(value, str) or parse_iso(value) is None:
            return None
        bound = parse_iso(localize_bound(value, zone, end=end))
        return bound.astimezone(zone) if bound is not None else None

    if time_max:
        return _bound(time_min), _bound(time_max, end=True)
    if _is_date_only(time_min):
        return _bound(time_min), _bound(time_min, end=True)
    return _bound(time_min), None


def _render_freebusy(result: str, args: dict[str, Any], language: str, zone: ZoneInfo | None = None) -> str:
    from pincer.voice.scheduling import parse_freebusy_output

    busy = parse_freebusy_output(result)
    if busy is None:
        return str(get_prompt("TOOL_ERROR", language) or "")
    if zone is not None:  # Google's slots are UTC; the caller hears the call's wall clock
        busy = [(start.astimezone(zone), end.astimezone(zone)) for start, end in busy]
    window_start, window_end = _freebusy_window(args, zone)
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


def _clean_web_text(value: Any) -> str:
    """Snippet text without URLs, HTML/Markdown markup, braces or ISO timestamps."""
    text = html.unescape(str(value or ""))
    text = _URL_RE.sub(" ", text)
    text = _MARKUP_RE.sub(" ", text)
    text = ISO_TIMESTAMP_RE.sub(" ", text)
    return " ".join(text.split()).strip(" .,;:-–—")


def _source_domain(url: Any) -> str:
    try:
        host = urlsplit(str(url or "")).hostname or ""
    except ValueError:
        return ""
    return host.removeprefix("www.")


def _truncate_words(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" .,;:-–—")
    return f"{cut}…" if cut else ""


def _render_websearch(result: str, language: str) -> str:
    """The websearch MCP returns JSON ``{"answer", "results": [{title, url,
    snippet}]}`` or ``{"error": ...}``. Each of the top results becomes
    'According to <domain>: <snippet>' (the title only stands in for an empty
    snippet), followed by the note that the snippets carry no date, all within
    WEBSEARCH_MAX_CHARS. The unsourced Tavily ``answer`` summary is left out.

    A failure is TOOL_NO_PROGRESS, not TOOL_ERROR: nothing is queued for a
    live lookup, so there's no "I'll sort that out afterwards" to promise.
    Output the bridge flagged with a ``[SECURITY NOTE: ...]`` is dropped:
    web text that looks like prompt injection never reaches the model."""
    no_progress = str(get_prompt("TOOL_NO_PROGRESS", language) or "")
    start = result.find("{")
    if start < 0:
        return no_progress
    if _SECURITY_NOTE in result[:start]:
        logger.warning("Web search result flagged as possible prompt injection; dropped")
        return no_progress
    try:
        data = json.loads(result[start:])
    except ValueError:
        data = None
    if not isinstance(data, dict) or data.get("error"):
        return no_progress
    found: list[tuple[str, str]] = []
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        text = _clean_web_text(item.get("snippet")) or _clean_web_text(item.get("title"))
        if not text:
            continue
        source = _source_domain(item.get("url")) or _tpl("websearch.unknown_source", language)
        found.append((source, text))
        if len(found) == WEBSEARCH_MAX_RESULTS:
            break
    if not found:
        return _tpl("websearch.none", language)
    template = _tpl("websearch.some", language)
    undated = _tpl("websearch.undated", language)
    # Each line gets an equal share of what the date note leaves, its
    # "According to <domain>: " and the separating space included.
    share = (WEBSEARCH_MAX_CHARS - len(undated) - 1) // len(found) - 1
    lines = []
    for source, text in found:
        room = share - len(template.format(source=source, text="")) - 1
        line = template.format(source=source, text=_truncate_words(text, max(room, 0)))
        lines.append(line if line.endswith((".", "!", "?", "…")) else f"{line}.")
    return " ".join([*lines, undated])


def render(
    tool_name: str, result: str, language: str, args: dict[str, Any] | None = None, zone: ZoneInfo | None = None
) -> str:
    """Deterministic, speakable rendering of a tool result (§7). On a call, ``zone`` is the call's
    zone: offset-less times in ``args`` are read, and times spoken, in it."""
    args = args or {}
    result = str(result or "")
    try:
        if tool_name == "websearch__search":
            text = _render_websearch(result, language)
        elif _is_error(result):
            text = str(get_prompt("TOOL_ERROR", language) or "")
        elif tool_name == "google__check_freebusy":
            text = _render_freebusy(result, args, language, zone)
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
        elif tool_name == "call_memory_search":
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


def render_duplicate(language: str) -> str:
    """What the model is told instead of re-running a call it already made this turn."""
    return ensure_speakable(_tpl("duplicate", language), language)


# ── Action description for VERIFY_ACTION ─────────────────────────────


def describe_action(tool_name: str, args: dict[str, Any] | None, language: str) -> str:
    """The exact commitment spoken before a write: 'den Termin Beratung am
    Dienstag, der achtzehnte August um vierzehn Uhr eintragen'."""
    args = args or {}
    table = get_prompt("ACTION_DESCRIPTIONS", language) or {}
    template = str(table.get(tool_name) or table.get("default") or "{tool}")
    start = parse_iso(args.get("start"))
    if start and _is_date_only(args.get("start")) and tool_name == "google__update_event" and not args.get("end"):
        # A new start alone keeps the stored duration, which the caller has not heard: no length.
        when = _tpl("when.from_day", language).format(day=spoken_datetime(start, language, include_time=False))
    elif start and _is_date_only(args.get("start")):
        end = parse_iso(args.get("end")) if _is_date_only(args.get("end")) else None
        when = _spoken_all_day(start, end, language)
    else:
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


def _is_date_only(value: Any) -> bool:
    return isinstance(value, str) and is_date_only(value)


def _spoken_all_day(first: datetime, end: datetime | None, language: str) -> str:
    """Bare dates (an all-day event): the days it covers. Google's end date is exclusive."""
    last = end - timedelta(days=1) if end is not None and end - first > timedelta(days=1) else first
    days = spoken_datetime(first, language, include_time=False)
    if last.date() != first.date():
        days += _RANGE_JOINERS.get(_lang(language), " to ") + spoken_datetime(last, language, include_time=False)
    return _tpl("when.all_day", language).format(days=days)


def _spoken_fallback_when(args: dict[str, Any], language: str) -> str:
    for key in ("when", "date", "time", "datetime"):
        dt = parse_iso(args.get(key))
        if dt is not None:
            return spoken_datetime(dt, language)
    return ""


__all__ = [
    "ISO_TIMESTAMP_RE",
    "RAW_DATA_RE",
    "WEBSEARCH_MAX_CHARS",
    "describe_action",
    "ensure_speakable",
    "is_speakable",
    "parse_iso",
    "render",
    "render_denied",
    "render_duplicate",
    "render_pending",
    "spoken_datetime",
    "spoken_range",
    "spoken_time",
]
