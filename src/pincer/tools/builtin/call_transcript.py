"""
On-demand call transcript retrieval (Sprint 3, T3.2).

"Show me the transcript of the last call" → the persisted transcript from the
voice call tables, PII-masked. Rows age out via the Sprint 0 retention purge,
so old transcripts legitimately disappear while memory notes keep the facts.
"""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite

from pincer.config import get_settings
from pincer.voice.pii_guard import mask_pii

logger = logging.getLogger(__name__)

MAX_LINES = 200


async def get_call_transcript(call_sid: str = "", context: dict[str, Any] | None = None) -> str:
    """Return the (PII-masked) transcript of a call.

    call_sid: Twilio Call SID; empty = the most recent call.
    """
    pincer_user_id = (context or {}).get("pincer_user_id", "")
    if not pincer_user_id:
        return "Error: no user identity available."

    settings = get_settings()
    db_path = str(settings.db_path)

    try:
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row

            if not call_sid.strip():
                rows = list(
                    await db.execute_fetchall(
                        "SELECT call_sid, to_number, started_at FROM voice_calls "
                        "WHERE pincer_user_id = ? ORDER BY started_at DESC LIMIT 1",
                        (pincer_user_id,),
                    )
                )
                if not rows:
                    return "No calls found."
                call_sid = rows[0]["call_sid"]

            call_rows = list(
                await db.execute_fetchall(
                    "SELECT call_sid, to_number, from_number, started_at FROM voice_calls "
                    "WHERE call_sid = ? AND pincer_user_id = ?",
                    (call_sid.strip(), pincer_user_id),
                )
            )
            if not call_rows:
                return f"No transcript found for call {call_sid}."
            entries = list(
                await db.execute_fetchall(
                    "SELECT speaker, text, timestamp FROM call_transcripts WHERE call_id = ? ORDER BY id LIMIT ?",
                    (call_sid.strip(), MAX_LINES),
                )
            )
    except aiosqlite.Error as e:
        logger.warning("Transcript lookup failed: %s", e)
        return "No call transcripts available yet."

    if not entries:
        return f"Call {call_sid} exists but has no stored transcript (it may have been purged by retention)."

    row = dict(call_rows[0])
    target = row.get("to_number") or row.get("from_number") or ""
    started = str(row.get("started_at", ""))[:16].replace("T", " ")
    header = f"Transcript of call {call_sid} ({target}, {started})"

    lines = [header, ""]
    for entry in entries:
        speaker = str(entry["speaker"]).upper()
        lines.append(f"{speaker}: {mask_pii(str(entry['text']))}")
    return "\n".join(lines)
