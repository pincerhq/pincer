"""
Post-call pipeline (Sprint 3): persist transcript → extract outcome → write
memory notes → report to the initiating user → propose follow-ups.

Runs after `_handle_call_end`; every step degrades gracefully — extraction
failure falls back to the basic summary, a missing memory store skips notes,
and the user always gets a final report message (T3.2). Follow-up suggestions
are proposals only: execution happens through the normal agent loop with its
approval gates when the user answers (PINCER_VOICE_AUTO_FOLLOWUP is reserved
and off by default).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import aiosqlite

from pincer.voice import status_notify
from pincer.voice.outcome import (
    CallOutcome,
    extract_outcome,
    render_fallback_report,
    render_report,
)

if TYPE_CHECKING:
    from pincer.llm.base import BaseLLMProvider
    from pincer.memory.base import BaseMemoryBackend
    from pincer.voice.engine import CallState
    from pincer.voice.transcript import TranscriptLogger

logger = logging.getLogger(__name__)

MEMORY_CATEGORY = "voice_call"


class PostCallProcessor:
    """Closes the loop after a call ends."""

    def __init__(
        self,
        settings: Any,
        llm: BaseLLMProvider | None = None,
        memory: BaseMemoryBackend | None = None,
        db_path: str | None = None,
    ) -> None:
        self._settings = settings
        self._llm = llm
        self._memory = memory
        self._db_path = str(db_path or getattr(settings, "db_path", "") or "")

    async def process(
        self,
        call_sid: str,
        state: CallState,
        transcript: TranscriptLogger | None,
        completed: bool,
        unverified_claims: list[str] | None = None,
    ) -> str:
        """Run the full pipeline; returns the report text that was sent."""
        info = status_notify.get_call_info(call_sid)
        language = (info.language if info and info.language else "") or state.language or "en"
        user_id = (info.user_id if info else "") or state.pincer_user_id or state.caller_number
        target_label = state.target_name or state.target_number or state.caller_number or "unknown"

        outcome: CallOutcome | None = None
        if transcript is not None and self._llm is not None:
            transcript_text = transcript.get_full_transcript()
            actions_text = "\n".join(
                f"{action.tool_name or action.action_type}: {action.output_summary[:200]}"
                for action in transcript.actions
            )
            outcome = await extract_outcome(
                self._llm,
                transcript_text,
                actions_text,
                language=language,
                purpose=state.purpose,
            )

        if outcome is not None and transcript is not None:
            # Audit trail: the structured outcome joins the call's action log
            transcript.log_action("outcome", output_summary=outcome.to_json())

        await self._persist(call_sid, state, transcript)

        if outcome is not None:
            await self._write_memory_notes(call_sid, user_id, target_label, outcome)

        if outcome is not None:
            report = render_report(outcome, target_label, state.duration_seconds, call_sid, language)
        else:
            report = render_fallback_report(target_label, state.duration_seconds, call_sid, completed, language)

        if unverified_claims:
            caveat = (
                "⚠️ Hinweis: Eine Erfolgsaussage im Gespräch konnte nicht durch ein Tool-Ergebnis belegt werden."
                if language.startswith("de")
                else "⚠️ Note: a completion claim made during the call could not be verified against tool results."
            )
            report = f"{report}\n{caveat}"

        if (
            outcome is not None
            and outcome.follow_up_suggestions
            and getattr(self._settings, "voice_auto_followup", False)
        ):
            # Reserved: auto-execution ships only once the reliability data
            # supports it. This build always proposes and waits for the user.
            logger.info("voice_auto_followup=true is reserved; proposing follow-ups instead of executing")

        # Final user message — replaces the plain "call ended" status (still ≤3 msgs/call)
        await status_notify.notify_stage(call_sid, status_notify.STAGE_ENDED, report)
        status_notify.clear_call(call_sid)

        return report

    # ── Persistence ───────────────────────────────────────

    async def _persist(self, call_sid: str, state: CallState, transcript: TranscriptLogger | None) -> None:
        """Save the call row + transcript + actions for /transcript and audit.
        Rows age out via the Sprint 0 retention purge."""
        if not self._db_path:
            return
        try:
            from pincer.voice.retention import ensure_voice_tables

            async with aiosqlite.connect(self._db_path) as db:
                await ensure_voice_tables(db)
                await db.execute(
                    "INSERT OR REPLACE INTO voice_calls "
                    "(call_sid, direction, from_number, to_number, pincer_user_id, started_at, ended_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        call_sid,
                        state.direction.value,
                        state.caller_number,
                        state.target_number,
                        state.pincer_user_id,
                        state.started_at.isoformat(),
                        (state.ended_at or datetime.now(UTC)).isoformat(),
                    ),
                )
                await db.commit()
                if transcript is not None:
                    await transcript.save_to_db(db)
        except Exception:
            logger.exception("Failed to persist call %s", call_sid)

    # ── Memory notes (T3.3) ───────────────────────────────

    async def _write_memory_notes(
        self,
        call_sid: str,
        user_id: str,
        target_label: str,
        outcome: CallOutcome,
    ) -> None:
        """Key facts and commitments land in cross-channel memory, tagged with
        the call. If the transcript is later purged (retention), the notes keep
        the fact — only the raw transcript reference goes stale."""
        if self._memory is None or not user_id:
            return
        tags = ["source:voice_call", f"call:{call_sid}"]
        try:
            for fact in outcome.key_facts:
                await self._memory.store_memory(
                    user_id=user_id,
                    content=f"[Call with {target_label}] {fact}",
                    category=MEMORY_CATEGORY,
                    extra_tags=tags,
                )
            for commitment in outcome.commitments:
                who = commitment.get("who", "callee")
                when = commitment.get("when")
                when_part = f" (by {when})" if when else ""
                await self._memory.store_memory(
                    user_id=user_id,
                    content=f"[Call with {target_label}] Commitment ({who}): {commitment.get('what', '')}{when_part}",
                    category=MEMORY_CATEGORY,
                    extra_tags=tags,
                )
            # Follow-up drafts are remembered so a later "yes, do it" turn can
            # recover the details and execute via the normal agent loop +
            # approval gates (T3.4).
            for suggestion in outcome.follow_up_suggestions:
                await self._memory.store_memory(
                    user_id=user_id,
                    content=(
                        f"[Call with {target_label}] Proposed follow-up: {suggestion.get('reason', '')} — "
                        f"tool={suggestion.get('tool', '')}, "
                        f"args={json.dumps(suggestion.get('draft_args', {}), ensure_ascii=False)}"
                    ),
                    category=MEMORY_CATEGORY,
                    extra_tags=[*tags, "followup"],
                )
        except Exception:
            logger.exception("Failed to write memory notes for call %s", call_sid)
