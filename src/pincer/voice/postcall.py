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

from pincer.voice import analytics as an
from pincer.voice import scheduling, status_notify, threads
from pincer.voice.briefing import briefing_from_state, report_adherence
from pincer.voice.outcome import (
    CallOutcome,
    extract_outcome,
    render_fallback_report,
    render_report,
)
from pincer.voice.transcript import Speaker

if TYPE_CHECKING:
    from pincer.llm.base import BaseLLMProvider
    from pincer.memory.base import BaseMemoryBackend
    from pincer.voice.engine import CallState
    from pincer.voice.transcript import TranscriptLogger

logger = logging.getLogger(__name__)

MEMORY_CATEGORY = "voice_call"


def _talk_time_context(call_analytics: an.CallAnalytics) -> str:
    """Speaking-time summary for the extraction prompt.

    Labelled with its method, because an estimate presented to the model as a
    measurement invites conclusions the data cannot carry.
    """
    if call_analytics.agent_speech_ms is None or call_analytics.caller_speech_ms is None:
        return ""
    parts = [
        f"agent spoke {call_analytics.agent_speech_ms / 1000:.0f}s",
        f"caller spoke {call_analytics.caller_speech_ms / 1000:.0f}s",
        f"silence {(call_analytics.silence_ms or 0) / 1000:.0f}s",
        f"{call_analytics.interruptions} interruption(s)",
    ]
    return f"SPEAKING TIME ({call_analytics.method}): " + ", ".join(parts)


class PostCallProcessor:
    """Closes the loop after a call ends."""

    def __init__(
        self,
        settings: Any,
        llm: BaseLLMProvider | None = None,
        memory: BaseMemoryBackend | None = None,
        db_path: str | None = None,
        tool_registry: Any = None,
    ) -> None:
        self._settings = settings
        self._llm = llm
        self._memory = memory
        self._db_path = str(db_path or getattr(settings, "db_path", "") or "")
        # Sprint 6: the appointment executor writes the calendar event through
        # the normal tool registry (OAuth, backoff, quota stay single-source).
        self._tools = tool_registry

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

        # Sprint 12: receptionist calls report to the OWNER with the §12 template
        if isinstance(state.metadata, dict) and state.metadata.get("receptionist") is True:
            return await self._process_receptionist(call_sid, state, transcript, completed)

        # Talk time is closed BEFORE extraction so the sentiment pass can see
        # it: long silences and a lopsided ratio are signals about how the call
        # went, and the extractor gets them as context rather than guessing.
        call_analytics = self._finalize_analytics(state, completed)

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
                talk_time=_talk_time_context(call_analytics),
            )

        if outcome is not None and transcript is not None:
            # Audit trail: the structured outcome joins the call's action log
            transcript.log_action("outcome", output_summary=outcome.to_json())

        await self._persist(call_sid, state, transcript)

        # T8.3: a callee who asked never to be called again is added to the
        # shared do-not-call list here, so the block applies to every user and
        # every channel from the next dial attempt onward.
        opt_out_note = await self._honor_opt_out(call_sid, state, transcript, outcome, language)

        if outcome is not None:
            await self._write_memory_notes(call_sid, user_id, target_label, outcome)

        # Did the agent actually open the call with the task it was given?
        # A smoke detector, never a gate: it only logs and counts.
        briefing = briefing_from_state(state)
        if briefing is not None:
            report_adherence(
                call_sid,
                briefing.task,
                transcript,
                task_result=outcome.task_result if outcome is not None else "",
            )

        # Sprint 13 §6: fold this call into its thread (rolling summary,
        # commitments, derived lifecycle step). Runs AFTER outcome extraction
        # because the outcome IS its input, and never blocks the report.
        thread_update = await self._update_thread(call_sid, outcome, language)

        if outcome is not None:
            report = render_report(outcome, target_label, state.duration_seconds, call_sid, language)
        else:
            report = render_fallback_report(target_label, state.duration_seconds, call_sid, completed, language)

        # Voicemail/not-connected endings stash their reason in metadata
        # instead of pre-empting the ENDED stage (which would drop this report)
        end_reason = str(state.metadata.get("end_reason") or "")
        if end_reason:
            report = f"{report}\nℹ️ {end_reason}"

        # Sprint 6: appointment scheduling calls get their post-call chain —
        # confirmed slot → idempotent calendar write + invitations; every
        # failure path lands in the report honestly, never silently.
        appointment_task = scheduling.get_appointment(call_sid)
        if appointment_task is not None:
            try:
                note = await scheduling.finalize_appointment(
                    self._tools, self._settings, appointment_task, call_sid, language
                )
            except Exception:
                logger.exception("Appointment finalization crashed [%s]", call_sid)
                note = (
                    "⚠️ Die Terminverarbeitung nach dem Anruf ist fehlgeschlagen — bitte Kalender manuell prüfen."
                    if language.startswith("de")
                    else "⚠️ Post-call appointment processing failed — please check the calendar manually."
                )
            if note:
                report = f"{report}\n{note}"
            scheduling.clear_appointment(call_sid)

        if opt_out_note:
            report = f"{report}\n{opt_out_note}"

        # Sprint 11 (§6.4 / §6.3): disclose autonomous writes verbatim, and
        # list what was deferred to a follow-up (approval timeout, tool
        # timeout/error) — the deferred items also land in memory as
        # follow-up drafts (T3.4), so "yes, do it" later can pick them up.
        autonomy_note = self._render_autonomous_actions(transcript, language)
        if autonomy_note:
            report = f"{report}\n{autonomy_note}"
        deferred = list(state.metadata.get("deferred_actions") or [])
        if deferred:
            report = f"{report}\n{self._render_deferred(deferred, language)}"
            await self._write_deferred_followups(call_sid, user_id, target_label, deferred)

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

        # Sentiment rides on the extraction that just ran — no second LLM call.
        an.apply_sentiment(call_analytics, outcome)
        await an.save_analytics(self._db_path, call_sid, call_analytics)
        an.record_metrics(
            call_analytics,
            engine=state.engine_type,
            direction=state.direction.value,
            language=language,
        )
        # §5: only a negative reading earns a line. A note on every call is
        # noise the owner learns to skip, and then misses the one that mattered.
        negative_line = an.render_report_line(call_analytics, language)
        if negative_line:
            report = f"{report}\n{negative_line}"

        # Sprint 13 §10: which matter this call belongs to, what is still
        # open, and whether the matter is now settled — wrapped around the
        # Sprint 3 report rather than replacing any of it.
        report = threads.decorate_report(report, thread_update, self._settings, language)

        # Final user message — replaces the plain "call ended" status (still ≤3 msgs/call)
        delivered = await status_notify.notify_stage(call_sid, status_notify.STAGE_ENDED, report)
        status_notify.clear_call(call_sid)

        # T9.5: the report-delivery SLI is the gap between hangup and this
        # moment. Stamped only on actual delivery — an undelivered report must
        # not be recorded as a fast one.
        if delivered:
            await self._stamp_report_delivered(call_sid)

        return report

    # ── Conversation analytics ────────────────────────────

    def _finalize_analytics(self, state: CallState, completed: bool) -> an.CallAnalytics:
        """Close the talk-time books for this call.

        A call nobody had a conversation on (voicemail, no answer) still gets a
        row — with null speech fields, saying "nothing to measure" rather than
        leaving the UI to guess whether zero means silent or unmeasured.
        """
        accumulator = an.get_accumulator(state)
        conversed = an.was_conversational(failure_code=str(state.metadata.get("failure_code", "") or ""))
        if accumulator is None:
            return an.CallAnalytics(
                method=an.METHOD_ESTIMATED,
                sentiment_reason=an.REASON_NOT_CONVERSED if not conversed else an.REASON_EXTRACTION_FAILED,
                created_at=datetime.now(UTC).isoformat(),
            )
        return accumulator.finalize(state.duration_seconds * 1000.0, conversed=conversed)

    # ── Call threads (Sprint 13) ──────────────────────────

    async def _update_thread(
        self,
        call_sid: str,
        outcome: CallOutcome | None,
        language: str,
    ) -> threads.ThreadUpdate | None:
        """Fold the finished call into its thread; None when threadless.

        Best effort on purpose: thread bookkeeping is a convenience layer over
        the call, and a failure in it must never cost the user the report they
        are waiting for.
        """
        try:
            manager = threads.get_thread_manager(self._settings)
            return await manager.update_after_call(call_sid, outcome=outcome, llm=self._llm, language=language)
        except Exception:
            logger.exception("Thread update failed for call %s", call_sid)
            return None

    async def _stamp_report_delivered(self, call_sid: str) -> None:
        if not self._db_path:
            return
        try:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(
                    "UPDATE voice_calls SET report_delivered_at = ? WHERE call_sid = ?",
                    (datetime.now(UTC).isoformat(), call_sid),
                )
                await db.commit()
        except Exception:
            logger.debug("report_delivered_at stamp failed [%s]", call_sid, exc_info=True)

    # ── Do-not-call opt-out (T8.3) ────────────────────────

    async def _honor_opt_out(
        self,
        call_sid: str,
        state: CallState,
        transcript: TranscriptLogger | None,
        outcome: CallOutcome | None,
        language: str,
    ) -> str:
        """Add the callee to the do-not-call list when they asked to be left alone.

        Reads the callee's own turns (never the agent's, which quotes the
        request back when apologising) plus the extractor's key facts, so both
        the deterministic transcript signal and the LLM's reading count.
        """
        number = state.target_number or state.caller_number
        if not number:
            return ""

        sources: list[str] = []
        if transcript is not None:
            sources.extend(entry.text for entry in transcript.entries if entry.speaker != Speaker.AGENT)
        if outcome is not None:
            sources.extend(outcome.key_facts)
            sources.append(outcome.task_result)
        haystack = "\n".join(t for t in sources if t)

        try:
            from pincer.voice.safety_gates import honor_opt_out

            added = await honor_opt_out(self._settings, number, haystack, call_sid=call_sid)
        except Exception:
            logger.exception("Do-not-call opt-out handling failed [%s]", call_sid)
            return ""
        if not added:
            return ""

        from pincer.voice.pii_guard import mask_phone_number

        masked = mask_phone_number(number)
        logger.warning("Callee opted out during call %s — %s added to do-not-call list", call_sid, masked)
        if language.startswith("de"):
            return f"🚫 {masked} hat um keinen weiteren Kontakt gebeten und steht jetzt auf der Sperrliste."
        if language.startswith("uk"):
            return f"🚫 {masked} попросив більше не телефонувати — номер додано до списку заборонених."
        return f"🚫 {masked} asked not to be called again and has been added to the do-not-call list."

    # ── Receptionist (Sprint 12 §11/§12) ──────────────────

    async def _process_receptionist(
        self,
        call_sid: str,
        state: CallState,
        transcript: TranscriptLogger | None,
        completed: bool,
    ) -> str:
        from pincer.voice.receptionist import report as rp
        from pincer.voice.receptionist.profile import get_profile

        reception = dict(state.metadata.get("reception") or {})
        profile = get_profile()
        owner_language = str(getattr(self._settings, "voice_default_language", "") or "") or (
            profile.default_language if profile else "en"
        )

        # Abuse flag from the extractor (best effort; a failed extraction is simply "not abusive")
        abusive = False
        if transcript is not None and self._llm is not None and transcript.entries:
            try:
                outcome = await extract_outcome(
                    self._llm, transcript.get_full_transcript(), "", language=owner_language, purpose="inbound"
                )
                abusive = bool(outcome.abusive) if outcome is not None else False
            except Exception:
                logger.debug("receptionist outcome extraction failed [%s]", call_sid, exc_info=True)

        await self._persist(call_sid, state, transcript)
        delivered = False
        report = ""
        if rp.should_report(reception) or abusive:
            await rp.persist_inbound_message(self._db_path, call_sid, reception)
            report = rp.render_owner_report(
                reception,
                call_sid=call_sid,
                profile=profile,
                language=owner_language,
                ended_at=state.ended_at,
                abusive=abusive,
                caller_number=state.caller_number,
            )
            delivered = await rp.deliver_owner_report(self._settings, call_sid, report)
            if delivered:
                await rp.stamp_delivered(self._db_path, call_sid)
                await self._stamp_report_delivered(call_sid)
        else:
            # Even a pure FAQ call keeps its intent on the call row
            await rp.persist_inbound_message(self._db_path, call_sid, reception) if reception.get("intent") else None
        status_notify.clear_call(call_sid)
        return report

    # ── In-call tool disclosure (Sprint 11) ───────────────

    @staticmethod
    def _render_autonomous_actions(transcript: TranscriptLogger | None, language: str) -> str:
        """§6.4: every `off`-mode write, verbatim, under a fixed heading."""
        if transcript is None:
            return ""
        rows = [a for a in transcript.actions if a.action_type == "tool_execute" and a.approval_mode == "off"]
        if not rows:
            return ""
        if language.startswith("de"):
            header = "🤖 Während des Anrufs eigenständig ausgeführt:"
        elif language.startswith("uk"):
            header = "🤖 Виконано самостійно під час дзвінка:"
        else:
            header = "🤖 Executed autonomously during the call:"
        lines = [header]
        for action in rows:
            detail = f" — {action.output_summary}" if action.output_summary else ""
            args = f" {action.input_summary}" if action.input_summary else ""
            lines.append(f"  • {action.tool_name}{args}{detail}")
        return "\n".join(lines)

    @staticmethod
    def _render_deferred(deferred: list[dict[str, Any]], language: str) -> str:
        if language.startswith("de"):
            header = "⏳ Im Anruf zurückgestellt (Vorschlag zur Nachbearbeitung):"
        elif language.startswith("uk"):
            header = "⏳ Відкладено під час дзвінка (пропозиція для подальших дій):"
        else:
            header = "⏳ Deferred during the call (suggested follow-up):"
        lines = [header]
        for item in deferred:
            summary = str(item.get("summary") or item.get("tool") or "")
            reason = str(item.get("reason") or "")
            lines.append(f"  • {summary} ({reason})" if reason else f"  • {summary}")
        return "\n".join(lines)

    async def _write_deferred_followups(
        self,
        call_sid: str,
        user_id: str,
        target_label: str,
        deferred: list[dict[str, Any]],
    ) -> None:
        """Deferred in-call actions become follow-up drafts in memory — the
        same T3.4 shape the outcome extractor uses, so the agent loop (with
        its approval gates) can execute them when the user says so."""
        if self._memory is None or not user_id:
            return
        tags = ["source:voice_call", f"call:{call_sid}", "followup", "deferred_in_call"]
        try:
            for item in deferred:
                await self._memory.store_memory(
                    user_id=user_id,
                    content=(
                        f"[Call with {target_label}] Proposed follow-up (deferred during the call, "
                        f"{item.get('reason', '')}): {item.get('summary', '')} — "
                        f"tool={item.get('tool', '')}, "
                        f"args={json.dumps(item.get('draft_args', {}), ensure_ascii=False)}"
                    ),
                    category=MEMORY_CATEGORY,
                    extra_tags=tags,
                )
        except Exception:
            logger.exception("Failed to write deferred follow-ups for call %s", call_sid)

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
                # Sprint 9 (T9.3): failure_code, engine, and language ride
                # along on the same row — they are what the golden signals and
                # the weekly digest group by, and writing them here keeps the
                # call row the single source of truth for "how did it go".
                await db.execute(
                    "INSERT OR REPLACE INTO voice_calls "
                    "(call_sid, direction, from_number, to_number, pincer_user_id, started_at, ended_at, "
                    "failure_code, engine, language) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        call_sid,
                        state.direction.value,
                        state.caller_number,
                        state.target_number,
                        state.pincer_user_id,
                        state.started_at.isoformat(),
                        (state.ended_at or datetime.now(UTC)).isoformat(),
                        str(state.metadata.get("failure_code", "") or ""),
                        state.engine_type,
                        state.language,
                    ),
                )
                # Sprint 13: INSERT OR REPLACE rewrites the whole row, so the
                # thread columns have to be re-derived from call_thread_members
                # (the durable membership record) rather than carried along.
                # The same statement backfills the member row's start date, so
                # a purged call still shows a date in its thread.
                await db.execute(
                    "UPDATE voice_calls SET "
                    "thread_id = COALESCE((SELECT m.thread_id FROM call_thread_members m "
                    "                      WHERE m.call_sid = voice_calls.call_sid), ''), "
                    "thread_attach_kind = COALESCE((SELECT m.attach_kind FROM call_thread_members m "
                    "                               WHERE m.call_sid = voice_calls.call_sid), '') "
                    "WHERE call_sid = ?",
                    (call_sid,),
                )
                await db.execute(
                    "UPDATE call_thread_members SET call_started_at = ?, direction = ? WHERE call_sid = ?",
                    (state.started_at.isoformat(), state.direction.value, call_sid),
                )
                # The briefing is stored verbatim so the dashboard can show
                # exactly what the agent was told — not our rendering of it.
                briefing = briefing_from_state(state)
                if briefing is not None:
                    await db.execute(
                        "UPDATE voice_calls SET briefing_json = ? WHERE call_sid = ?",
                        (briefing.to_json(), call_sid),
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
