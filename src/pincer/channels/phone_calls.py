"""
Voice channel — phone calls as a first-class Pincer channel.

Implements BaseChannel for voice, mapping Twilio calls to the same
messaging abstraction used by Telegram, WhatsApp, and Discord.
Voice sessions share memory with text channels via cross-channel identity.

Sprint 1 hardening: every call gets a driven CallStateMachine with an enforced
timeout watchdog (a timeout is a spoken, polite exit — never silence), LLM
errors escalate to a graceful goodbye instead of looping, a TranscriptLogger
records both sides for the post-call truthfulness assertion, and call end
always leaves the machine in COMPLETED/FAILED and messages the initiating user.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from pincer.channels.base import BaseChannel, ChannelType, IncomingMessage, MessageHandler
from pincer.voice.metrics import VoiceMetricsRegistry
from pincer.voice.state_machine import CallPhase, CallStateMachine
from pincer.voice.transcript import Speaker, TranscriptLogger

if TYPE_CHECKING:
    from pincer.config import Settings
    from pincer.voice.engine import CallState, VoiceEngine

logger = logging.getLogger(__name__)

# Phases whose timeout still counts as a successful call outcome
_BENIGN_TIMEOUT_PHASES = {CallPhase.CONFIRM, CallPhase.ENDING}

# Consecutive agent-brain errors tolerated before ending the call gracefully
MAX_CONSECUTIVE_ERRORS = 2

WATCHDOG_INTERVAL_S = 2.0


class VoiceChannel(BaseChannel):
    """Phone call channel — bridges Twilio voice to the Pincer agent."""

    channel_type = ChannelType.VOICE

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._handler: MessageHandler | None = None
        self._engine: VoiceEngine | None = None
        self._state_machines: dict[str, CallStateMachine] = {}
        self._response_queues: dict[str, asyncio.Queue[str]] = {}
        self._transcripts: dict[str, TranscriptLogger] = {}
        self._error_counts: dict[str, int] = {}
        self._watchdog_task: asyncio.Task[None] | None = None
        self._post_call_processor: Any = None
        self._postcall_tasks: dict[str, asyncio.Task[Any]] = {}
        self.metrics = VoiceMetricsRegistry()

    @property
    def name(self) -> str:
        return "voice"

    def set_engine(self, engine: VoiceEngine) -> None:
        self._engine = engine
        engine.set_on_speech(self._handle_speech)
        engine.set_on_call_end(self._handle_call_end)
        engine.metrics_registry = self.metrics

    def set_post_call_processor(self, processor: Any) -> None:
        """Install the Sprint 3 post-call pipeline (report/memory/follow-ups)."""
        self._post_call_processor = processor

    async def start(self, handler: MessageHandler) -> None:
        self._handler = handler
        logger.info("Voice channel started")

    async def stop(self) -> None:
        if self._watchdog_task:
            self._watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watchdog_task
            self._watchdog_task = None
        # Let in-flight post-call reports finish (they are quick and user-facing)
        for task in list(self._postcall_tasks.values()):
            with contextlib.suppress(Exception):
                await task
        if self._engine:
            for call_sid in list(self._engine.get_active_calls()):
                try:
                    await self._engine.end_call(call_sid)
                except Exception:
                    logger.exception("Error ending call %s during shutdown", call_sid)
        logger.info("Voice channel stopped")

    async def send(self, user_id: str, text: str, **kwargs: Any) -> None:
        """Send a text response to the active voice call for this user.

        In voice mode, the engine converts the text to speech. Raises when
        there's no active call to speak into, instead of logging and
        swallowing — the live in-call reply path (`_handle_speech`) always
        has `call_sid` on hand and speaks via the engine directly, so it never
        goes through this method; only tool-invoked sends (send_file,
        send_image, generate_image) use the user_id lookup below, and those
        need to know delivery didn't happen rather than reporting false
        success back to the LLM (issue #162).
        """
        call_sid = kwargs.get("call_sid", "")
        if not call_sid:
            call_sid = self._find_active_call_for_user(user_id)

        if not call_sid or not self._engine:
            raise RuntimeError(f"No active call for user {user_id!r} to send speech")

        await self._engine.send_speech(call_sid, text)

    # ── Call tracking ─────────────────────────────────────

    def _ensure_call_tracking(self, call_sid: str, state: CallState) -> CallStateMachine:
        """Lazily create the state machine, transcript, and metrics for a call."""
        sm = self._state_machines.get(call_sid)
        if sm is None:
            from pincer.voice.engine import CallDirection

            sm = CallStateMachine(call_sid, is_outbound=state.direction == CallDirection.OUTBOUND)
            sm.start_call()
            self._state_machines[call_sid] = sm
        if call_sid not in self._transcripts:
            self._transcripts[call_sid] = TranscriptLogger(call_sid)
        self.metrics.get_or_start(call_sid, engine=state.engine_type)
        self._start_watchdog()
        return sm

    def get_transcript(self, call_sid: str) -> TranscriptLogger | None:
        return self._transcripts.get(call_sid)

    def _start_watchdog(self) -> None:
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._watchdog_loop(), name="voice-watchdog")

    async def _watchdog_loop(self) -> None:
        """Enforce PHASE_TIMEOUTS: a timed-out phase gets a spoken, polite exit
        and a graceful hangup — never silence, never a stuck state machine."""
        try:
            while True:
                await asyncio.sleep(WATCHDOG_INTERVAL_S)
                for call_sid, sm in list(self._state_machines.items()):
                    if sm.is_terminal or not sm.check_timeout():
                        continue
                    await self._handle_phase_timeout(call_sid, sm)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Voice watchdog error")

    def _call_language(self, call_sid: str) -> tuple[str, str]:
        """(language, de_formality) for a call — for localized canned lines."""
        from pincer.voice.language import de_formality

        state = self._engine.get_call_state(call_sid) if self._engine else None
        language = str(getattr(state, "language", "") or "en") if state else "en"
        return language[:2], de_formality(self._settings)

    async def _handle_phase_timeout(self, call_sid: str, sm: CallStateMachine) -> None:
        phase = sm.phase
        language, formality = self._call_language(call_sid)
        message = sm.get_timeout_message(language, formality)
        logger.info("Phase timeout [%s]: %s — speaking exit and ending call", call_sid, phase)

        transcript = self._transcripts.get(call_sid)
        if transcript:
            transcript.log_utterance(Speaker.AGENT, message, state=str(phase))

        if self._engine:
            with contextlib.suppress(Exception):
                await self._engine.send_speech(call_sid, message)

        terminal = CallPhase.COMPLETED if phase in _BENIGN_TIMEOUT_PHASES else CallPhase.FAILED
        sm.force_terminal(terminal, reason=f"timeout_{phase.value}")

        if self._engine:
            with contextlib.suppress(Exception):
                await self._engine.end_call(call_sid)

    # ── Live conversation ─────────────────────────────────

    async def _handle_speech(self, call_sid: str, text: str) -> None:
        """Called when the caller speaks (STT output or ConversationRelay text)."""
        if not self._handler:
            return

        state = self._engine.get_call_state(call_sid) if self._engine else None
        if not state:
            return

        sm = self._ensure_call_tracking(call_sid, state)
        if sm.is_terminal:
            return

        metrics = self.metrics.get_or_start(call_sid, engine=state.engine_type)
        metrics.mark_caller_utterance()

        transcript = self._transcripts[call_sid]
        transcript.log_utterance(Speaker.CALLER, text, state=str(sm.phase))

        # The greeting phases end the moment the callee speaks
        if sm.phase in (CallPhase.GREETING, CallPhase.OUTBOUND_GREETING):
            sm.transition(CallPhase.INTENT_CAPTURE, "caller_spoke")

        incoming = IncomingMessage(
            user_id=state.caller_number,
            channel="voice",
            text=text,
            channel_type=ChannelType.VOICE,
        )

        try:
            response = await self._handler(incoming)
            self._error_counts.pop(call_sid, None)
            if response and self._engine:
                metrics.mark_agent_speech_start()
                delivered = await self._engine.send_speech(call_sid, response)
                # Honest transcripts: "text generated" is not "audio heard".
                # An undelivered agent turn is marked so post-call summaries
                # and the truthfulness check reflect what the caller heard.
                transcript.log_utterance(
                    Speaker.AGENT,
                    response,
                    state=str(sm.phase) if delivered is not False else "undelivered",
                )
        except Exception:
            logger.exception("Error handling voice input for call %s", call_sid)
            await self._handle_brain_error(call_sid, sm)

    async def _handle_brain_error(self, call_sid: str, sm: CallStateMachine) -> None:
        """LLM/tool failure mid-call: apologize; after repeated failures, end
        the call gracefully instead of looping (T1.3)."""
        from pincer.voice.prompts import get_prompt

        errors = self._error_counts.get(call_sid, 0) + 1
        self._error_counts[call_sid] = errors
        transcript = self._transcripts.get(call_sid)
        language, formality = self._call_language(call_sid)

        if errors >= MAX_CONSECUTIVE_ERRORS:
            apology = get_prompt("BRAIN_ERROR_FINAL", language, formality)
            if transcript:
                transcript.log_utterance(Speaker.AGENT, apology, state=str(sm.phase))
            if self._engine:
                with contextlib.suppress(Exception):
                    await self._engine.send_speech(call_sid, apology)
            sm.force_terminal(CallPhase.FAILED, reason="repeated_errors")
            if self._engine:
                with contextlib.suppress(Exception):
                    await self._engine.end_call(call_sid)
            return

        retry_line = get_prompt("BRAIN_ERROR_RETRY", language, formality)
        if transcript:
            transcript.log_utterance(Speaker.AGENT, retry_line, state=str(sm.phase))
        if self._engine:
            with contextlib.suppress(Exception):
                await self._engine.send_speech(call_sid, retry_line)

    # ── Call end ──────────────────────────────────────────

    async def _handle_call_end(self, call_sid: str, state: CallState) -> None:
        """Called when a call ends — cleanup must always complete: terminal
        state machine, truthfulness check, metrics, and a final user status."""
        sm = self._state_machines.pop(call_sid, None)
        transcript = self._transcripts.pop(call_sid, None)
        self._response_queues.pop(call_sid, None)
        self._error_counts.pop(call_sid, None)

        completed = True
        if sm is not None:
            if not sm.is_terminal:
                # Hangup / provider failure before a clean ENDING
                terminal = CallPhase.COMPLETED if sm.phase in _BENIGN_TIMEOUT_PHASES else CallPhase.FAILED
                sm.force_terminal(terminal, reason="call_end_cleanup")
            completed = sm.phase == CallPhase.COMPLETED

        unverified: list[str] = []
        if transcript:
            unverified = transcript.verify_completion_claims()

        self.metrics.finish_call(call_sid)

        logger.info(
            "Call ended: %s (%s, %ds, %s)",
            call_sid,
            state.direction,
            state.duration_seconds,
            "completed" if completed else "failed",
        )

        # Sprint 3: full post-call pipeline (report in the user's language,
        # memory notes, follow-up proposals). Runs as a background task so
        # engine cleanup isn't blocked; the pipeline sends the final message.
        if self._post_call_processor is not None:
            task = asyncio.create_task(
                self._run_post_call(call_sid, state, transcript, completed, unverified),
                name=f"postcall-{call_sid}",
            )
            self._postcall_tasks[call_sid] = task
            task.add_done_callback(lambda _t: self._postcall_tasks.pop(call_sid, None))
            return

        # T1.5 fallback (no processor wired): plain final status, even on failure
        from pincer.voice.status_notify import notify_ended

        outcome = f"{'completed' if completed else 'did not complete'} ({state.duration_seconds}s)"
        if unverified:
            outcome += " — note: the agent made a completion claim I could not verify against tool results"
        with contextlib.suppress(Exception):
            await notify_ended(call_sid, outcome)

    async def _run_post_call(
        self,
        call_sid: str,
        state: CallState,
        transcript: TranscriptLogger | None,
        completed: bool,
        unverified: list[str],
    ) -> None:
        try:
            await self._post_call_processor.process(call_sid, state, transcript, completed, unverified)
        except Exception:
            logger.exception("Post-call pipeline failed for %s — sending fallback status", call_sid)
            from pincer.voice.status_notify import notify_ended

            with contextlib.suppress(Exception):
                await notify_ended(call_sid, "completed" if completed else "did not complete")

    def _find_active_call_for_user(self, user_id: str) -> str:
        """Find the active call SID for a given user."""
        if not self._engine:
            return ""
        for call_sid, state in self._engine.get_active_calls().items():
            if state.caller_number == user_id or state.user_id == user_id:
                return call_sid
        return ""

    def get_state_machine(self, call_sid: str) -> CallStateMachine | None:
        return self._state_machines.get(call_sid)

    def set_state_machine(self, call_sid: str, sm: CallStateMachine) -> None:
        self._state_machines[call_sid] = sm
