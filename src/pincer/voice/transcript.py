"""
Call transcript and audit log — real-time transcript logging, post-call
report generation, and integration with the audit system.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pincer.services.voice import CallsService

logger = logging.getLogger(__name__)

# T1.4: agent utterances that claim a task was completed. If no successful
# CallAction backs such a claim, the call is flagged (feeds the reliability
# metric — the agent must never claim success the tools didn't confirm).
COMPLETION_CLAIM_PATTERNS = re.compile(
    r"\b("
    r"i(?:'ve| have) (?:booked|scheduled|sent|cancelled|canceled|ordered|created|updated|confirmed|rescheduled)"
    r"|(?:it's|it is|that's|that is) (?:booked|scheduled|sent|cancelled|canceled|done|confirmed|all set)"
    r"|(?:your|the) \w+ (?:is|has been) (?:booked|scheduled|sent|cancelled|canceled|created|updated|confirmed)"
    r"|all (?:set|done|taken care of)"
    r"|successfully (?:booked|scheduled|sent|cancelled|canceled|ordered|created|updated)"
    # German (Sprint 2)
    r"|ist (?:bestätigt|gebucht|erledigt|storniert|abgesagt|eingetragen|verschickt|gesendet)"
    r"|habe ich (?:gebucht|bestätigt|storniert|abgesagt|eingetragen|verschickt|gesendet|erledigt)"
    r"|ich habe (?:den|die|das|ihren|ihre|ihr)? ?\w* "
    r"?(?:gebucht|bestätigt|storniert|abgesagt|eingetragen|verschickt|gesendet)"
    r"|(?:alles|das wäre) erledigt"
    r")\b",
    re.IGNORECASE,
)


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
    # The last agent line of a provider utterance (sent with last=True). A
    # streamed turn spans several; see mark_interrupted().
    ends_utterance: bool = False


@dataclass
class CallAction:
    action_type: str  # tool_call, tool_execute, tool_denied, dtmf, transfer, confirm, outcome
    tool_name: str = ""
    input_summary: str = ""
    output_summary: str = ""
    user_confirmed: bool | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    # Sprint 11 (in-call tools): policy tier (R/W/X), the approval mode the
    # action ran under (auto/verbal/user/off), and the stable deny reason
    # code (§5.2) for tool_denied rows.
    tier: str = ""
    approval_mode: str = ""
    deny_reason: str = ""


# Action types whose non-error output backs a completion claim (T1.4 / §7).
SUCCESSFUL_ACTION_TYPES = frozenset({"tool_call", "tool_execute"})


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
        position: int | None = None,
    ) -> None:
        """Append a line, or with `position` insert it there: a line whose
        send was cancelled is logged where the send began, before whatever
        was logged while it was in flight."""
        entry = TranscriptEntry(
            speaker=speaker,
            text=text,
            confidence=confidence,
            is_final=is_final,
            state=state,
        )
        if position is None:
            self._entries.append(entry)
        else:
            self._entries.insert(position, entry)
        logger.debug("[%s] %s: %s", self._call_sid, speaker, text[:100])

    def end_agent_utterance(self) -> None:
        """The agent line just logged closes a provider utterance."""
        if self._entries and self._entries[-1].speaker == Speaker.AGENT:
            self._entries[-1].ends_utterance = True

    def mark_interrupted(self, heard: str) -> int:
        """Re-mark the agent lines of the current reply the caller did not hear.

        `heard` is what the provider says it played before the caller cut in
        (ConversationRelay's `utteranceUntilInterrupt`), and it covers only the
        utterance being played — a streamed turn spans several (a filler, early
        sentences sent with last=True). That utterance is the latest one `heard`
        is the start of — searched from the end, because a short earlier line
        ("Ja.") is easily a prefix of what was really playing ("Ja, der
        Termin…") — else the latest. Utterances before it played in full
        and keep their state; inside it, lines `heard` contains keep theirs,
        the first one it does not was cut off ("interrupted"), and every later
        line never played ("undelivered"). Returns how many lines were re-marked.
        """

        def _norm(value: str) -> str:
            return " ".join(value.split()).casefold()

        reply: list[TranscriptEntry] = []
        for entry in reversed(self._entries):
            if entry.speaker == Speaker.CALLER:
                break
            if entry.speaker == Speaker.AGENT:
                reply.append(entry)
        reply.reverse()
        utterances: list[list[TranscriptEntry]] = []
        for index, entry in enumerate(reply):
            if index == 0 or reply[index - 1].ends_utterance:
                utterances.append([])
            utterances[-1].append(entry)
        if not utterances:
            return 0

        heard_norm = _norm(heard)
        playing = len(utterances) - 1
        if heard_norm:
            for index in range(len(utterances) - 1, -1, -1):
                utterance = utterances[index]
                text = _norm(" ".join(entry.text for entry in utterance))
                if text.startswith(heard_norm) or heard_norm.startswith(_norm(utterance[0].text)):
                    playing = index
                    break

        marked = 0
        cut = False
        for offset, utterance in enumerate(utterances[playing:]):
            for entry in utterance:
                if not cut and offset == 0 and _norm(entry.text) in heard_norm:
                    continue
                if entry.state == "undelivered":
                    continue
                entry.state = "undelivered" if cut else "interrupted"
                cut = True
                marked += 1
        return marked

    def log_action(
        self,
        action_type: str,
        tool_name: str = "",
        input_summary: str = "",
        output_summary: str = "",
        user_confirmed: bool | None = None,
        *,
        tier: str = "",
        approval_mode: str = "",
        deny_reason: str = "",
    ) -> CallAction:
        action = CallAction(
            action_type=action_type,
            tool_name=tool_name,
            input_summary=input_summary,
            output_summary=output_summary,
            user_confirmed=user_confirmed,
            tier=tier,
            approval_mode=approval_mode,
            deny_reason=deny_reason,
        )
        self._actions.append(action)
        return action

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
                parts.append(f"  - {action.action_type}: {action.tool_name or 'N/A'}{confirmed_str}")
                if action.output_summary:
                    parts.append(f"    Result: {action.output_summary[:200]}")
            parts.append("")

        parts.append("Transcript:")
        for entry in self._entries:
            if not entry.is_final:
                continue
            parts.append(f"  {entry.speaker}: {entry.text}")

        return "\n".join(parts)

    def _has_successful_action(self) -> bool:
        """True if at least one tool action ran with a non-error result."""
        for action in self._actions:
            if action.action_type not in SUCCESSFUL_ACTION_TYPES:
                continue
            if action.user_confirmed is False:
                continue
            output = action.output_summary.strip()
            if output and not output.lower().startswith("error"):
                return True
        return False

    def verify_completion_claims(self) -> list[str]:
        """Post-call truthfulness assertion (Sprint 1, T1.4).

        Returns agent utterances that claim task completion without a matching
        successful CallAction, and logs a WARNING for each. An empty list means
        the transcript is honest (or made no completion claims).
        """
        claims = [
            entry.text
            for entry in self._entries
            if entry.is_final and entry.speaker == Speaker.AGENT and COMPLETION_CLAIM_PATTERNS.search(entry.text)
        ]
        if not claims:
            return []
        if self._has_successful_action():
            return []
        for claim in claims:
            logger.warning(
                "Unverified completion claim [%s]: agent said %r but no successful tool action exists",
                self._call_sid,
                claim[:200],
            )
        return claims

    async def save_to_db(self, calls: CallsService) -> None:
        """Persist transcript and actions to the database."""
        await calls.add_transcript_lines(
            [
                {
                    "call_id": self._call_sid,
                    "speaker": entry.speaker,
                    "text": entry.text,
                    "confidence": entry.confidence,
                    "is_final": entry.is_final,
                    "state": entry.state,
                    "timestamp": entry.timestamp,
                }
                for entry in self._entries
                if entry.is_final
            ]
        )
        await calls.add_actions(
            [
                {
                    "call_id": self._call_sid,
                    "action_type": action.action_type,
                    "tool_name": action.tool_name,
                    "input_summary": action.input_summary,
                    "output_summary": action.output_summary,
                    "user_confirmed": action.user_confirmed,
                    "timestamp": action.timestamp,
                    "tier": action.tier,
                    "approval_mode": action.approval_mode,
                    "deny_reason": action.deny_reason,
                }
                for action in self._actions
            ]
        )
        logger.info(
            "Transcript saved for %s: %d entries, %d actions",
            self._call_sid,
            len(self._entries),
            len(self._actions),
        )
