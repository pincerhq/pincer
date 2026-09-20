"""
On-demand call transcript retrieval (Sprint 3, T3.2).

"Show me the transcript of the last call" → the persisted transcript from the
voice call tables, PII-masked. Rows age out via the Sprint 0 retention purge,
so old transcripts legitimately disappear while memory notes keep the facts.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from pincer.config import get_settings
from pincer.db.engine import get_database_url
from pincer.services.voice import CallsService
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
        calls = CallsService(get_database_url(Path(db_path)))

        if not call_sid.strip():
            newest = await calls.newest_for_user(pincer_user_id)
            if newest is None:
                return "No calls found."
            call_sid = str(newest["call_sid"])

        # Scoped to the caller: a Call SID is not an authorisation.
        call = await calls.get_for_user(call_sid.strip(), pincer_user_id)
        if call is None:
            return f"No transcript found for call {call_sid}."
        entries = await calls.transcript_for(call_sid.strip(), limit=MAX_LINES)
    except SQLAlchemyError as e:
        logger.warning("Transcript lookup failed: %s", e)
        return "No call transcripts available yet."

    if not entries:
        return f"Call {call_sid} exists but has no stored transcript (it may have been purged by retention)."

    row = call
    target = row.get("to_number") or row.get("from_number") or ""
    started = str(row.get("started_at", ""))[:16].replace("T", " ")
    header = f"Transcript of call {call_sid} ({target}, {started})"

    lines = [header, ""]
    for entry in entries:
        speaker = str(entry["speaker"]).upper()
        lines.append(f"{speaker}: {mask_pii(str(entry['text']))}")
    return "\n".join(lines)
