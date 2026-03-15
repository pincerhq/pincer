"""
Call transcript and audit log — real-time transcript logging, post-call
report generation, and integration with the audit system.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:

    import aiosqlite

logger = logging.getLogger(__name__)


class Speaker:
    AGENT = "agent"
    CALLER = "caller"
    PROVIDER = "provider"
    SYSTEM = "system"


@dataclass
class TranscriptEntry:
    speaker: str
    text: str
    confidence: float = 1.0
    is_final: bool = True
    state: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass
class CallAction:
    action_type: str  # tool_call, dtmf, transfer, confirm
    tool_name: str = ""
    input_summary: str = ""
    output_summary: str = ""
    user_confirmed: bool | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


class TranscriptLogger:
    """Logs real-time transcripts and actions for a voice call."""

    def __init__(self, call_sid: str) -> None:
        self._call_sid = call_sid
        self._entries: list[TranscriptEntry] = []
        self._actions: list[CallAction] = []

    @property
    def call_sid(self) -> str:
        return self._call_sid

    @property
    def entries(self) -> list[TranscriptEntry]:
        return list(self._entries)

    @property
    def actions(self) -> list[CallAction]:
        return list(self._actions)

    def log_utterance(
        self,
        speaker: str,
        text: str,
        confidence: float = 1.0,
        is_final: bool = True,
        state: str = "",
    ) -> None:
        entry = TranscriptEntry(
            speaker=speaker,
            text=text,
            confidence=confidence,
            is_final=is_final,
            state=state,
        )
        self._entries.append(entry)
        logger.debug("[%s] %s: %s", self._call_sid, speaker, text[:100])

    def log_action(
        self,
        action_type: str,
        tool_name: str = "",
        input_summary: str = "",
        output_summary: str = "",
        user_confirmed: bool | None = None,
    ) -> None:
        action = CallAction(
            action_type=action_type,
            tool_name=tool_name,
            input_summary=input_summary,
            output_summary=output_summary,
            user_confirmed=user_confirmed,
        )
        self._actions.append(action)

    def get_full_transcript(self) -> str:
        """Return the full conversation transcript as readable text."""
        lines = []
        for entry in self._entries:
            if not entry.is_final:
                continue
            speaker = entry.speaker.upper()
            lines.append(f"[{entry.timestamp}] {speaker}: {entry.text}")
        return "\n".join(lines)

    def generate_summary(self) -> str:
        """Generate a post-call summary of the conversation and actions."""
        parts = [f"Call Transcript Summary ({self._call_sid})", ""]

        duration_entries = [e for e in self._entries if e.is_final]
        if duration_entries:
            first = duration_entries[0].timestamp
            last = duration_entries[-1].timestamp
            parts.append(f"Duration: {first} to {last}")
            parts.append(f"Utterances: {len(duration_entries)}")
            parts.append("")

        if self._actions:
            parts.append("Actions taken:")
            for action in self._actions:
                confirmed_str = ""
                if action.user_confirmed is not None:
                    confirmed_str = " (confirmed)" if action.user_confirmed else " (rejected)"
                parts.append(
                    f"  - {action.action_type}: {action.tool_name or 'N/A'}"
                    f"{confirmed_str}"
                )
                if action.output_summary:
                    parts.append(f"    Result: {action.output_summary[:200]}")
            parts.append("")

        parts.append("Transcript:")
        for entry in self._entries:
            if not entry.is_final:
                continue
            parts.append(f"  {entry.speaker}: {entry.text}")

        return "\n".join(parts)

    async def save_to_db(self, db: aiosqlite.Connection) -> None:
        """Persist transcript and actions to the database."""
        for entry in self._entries:
            if not entry.is_final:
                continue
            await db.execute(
                "INSERT INTO call_transcripts "
                "(call_id, speaker, text, confidence, is_final, state, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    self._call_sid,
                    entry.speaker,
                    entry.text,
                    entry.confidence,
                    entry.is_final,
                    entry.state,
                    entry.timestamp,
                ),
            )

        for action in self._actions:
            await db.execute(
                "INSERT INTO call_actions "
                "(call_id, action_type, tool_name, input_summary, "
                "output_summary, user_confirmed, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    self._call_sid,
                    action.action_type,
                    action.tool_name,
                    action.input_summary,
                    action.output_summary,
                    action.user_confirmed,
                    action.timestamp,
                ),
            )

        await db.commit()
        logger.info(
            "Transcript saved for %s: %d entries, %d actions",
            self._call_sid, len(self._entries), len(self._actions),
        )
