"""
Shared response-formatting helpers for Google Workspace tools.

All tool functions return plain strings.  These helpers produce consistent,
readable output from raw Google API response dicts.
"""

from __future__ import annotations

from typing import Any

# ── Generic ───────────────────────────────────────────────────────────────────

def truncate(text: str, max_chars: int = 200) -> str:
    """Truncate *text* to at most *max_chars* characters."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


def fmt_list(items: list[str], header: str = "", more: bool = False) -> str:
    """Format a list of strings with an optional header and 'more available' note."""
    parts: list[str] = []
    if header:
        parts.append(header)
    parts.extend(items)
    if more:
        parts.append("(more results available — use pagination parameters)")
    return "\n".join(parts)


# ── Gmail ─────────────────────────────────────────────────────────────────────

def fmt_message_summary(msg: dict[str, Any]) -> str:
    """One-line summary for a Gmail message (used in list results)."""
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    subject = headers.get("Subject", "(no subject)")
    sender = headers.get("From", "unknown")
    date = headers.get("Date", "")
    snippet = truncate(msg.get("snippet", ""), 120)
    msg_id = msg.get("id", "")
    return f"**{subject}**\n  From: {sender}\n  Date: {date}\n  {snippet}\n  ID: {msg_id}"


def fmt_message_full(msg: dict[str, Any], body: str) -> str:
    """Full message view for google__get_message."""
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    lines = [
        f"Subject: {headers.get('Subject', '(no subject)')}",
        f"From:    {headers.get('From', 'unknown')}",
        f"To:      {headers.get('To', '')}",
        f"Date:    {headers.get('Date', '')}",
        f"ID:      {msg.get('id', '')}",
        "",
        body or msg.get("snippet", "(no body)"),
    ]
    return "\n".join(lines)


# ── Calendar ──────────────────────────────────────────────────────────────────

def fmt_event(event: dict[str, Any]) -> str:
    """Format a single calendar event for display."""
    start = event.get("start", {})
    end = event.get("end", {})
    start_str = start.get("dateTime", start.get("date", ""))
    end_str = end.get("dateTime", end.get("date", ""))
    summary = event.get("summary", "(no title)")
    location = event.get("location", "")
    attendee_count = len(event.get("attendees", []))
    event_id = event.get("id", "")

    loc_part = f" | {location}" if location else ""
    att_part = f" | {attendee_count} attendee(s)" if attendee_count else ""
    return f"  {start_str} → {end_str} — {summary}{loc_part}{att_part}\n  ID: {event_id}"


# ── Drive ─────────────────────────────────────────────────────────────────────

def fmt_file(f: dict[str, Any]) -> str:
    """Format a Drive file entry."""
    name = f.get("name", "(unnamed)")
    mime = f.get("mimeType", "")
    modified = f.get("modifiedTime", "")
    size = f.get("size", "")
    file_id = f.get("id", "")
    size_part = f" ({size} bytes)" if size else ""
    return f"  {name}{size_part}\n  Type: {mime} | Modified: {modified}\n  ID: {file_id}"


# ── Sheets ────────────────────────────────────────────────────────────────────

def fmt_sheet_values(values: list[list[Any]], range_name: str = "") -> str:
    """Format spreadsheet values as a plain-text table."""
    if not values:
        return f"No data found{' in ' + range_name if range_name else ''}."
    header = f"Range: {range_name}\n" if range_name else ""
    rows = []
    for row in values:
        rows.append(" | ".join(str(cell) for cell in row))
    return header + "\n".join(rows)


# ── Tasks ─────────────────────────────────────────────────────────────────────

def fmt_task(task: dict[str, Any]) -> str:
    """Format a single task."""
    title = task.get("title", "(no title)")
    status = task.get("status", "needsAction")
    due = task.get("due", "")
    notes = truncate(task.get("notes", ""), 100)
    task_id = task.get("id", "")
    due_part = f" | Due: {due}" if due else ""
    notes_part = f"\n  Notes: {notes}" if notes else ""
    return f"  [{status}] {title}{due_part}\n  ID: {task_id}{notes_part}"
