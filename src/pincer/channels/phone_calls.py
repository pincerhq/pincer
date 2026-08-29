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
from pincer.voice import call_end
from pincer.voice.metrics import VoiceMetricsRegistry
from pincer.voice.state_machine import CallPhase, CallStateMachine
from pincer.voice.transcript import Speaker, TranscriptLogger

if TYPE_CHECKING:
    from pincer.config import Settings
    from pincer.voice.engine import CallState, VoiceEngine
    from pincer.voice.in_call_tools import InCallToolGate
    from pincer.voice.receptionist.session import ReceptionSession

logger = logging.getLogger(__name__)

# Phases whose timeout still counts as a successful call outcome
_BENIGN_TIMEOUT_PHASES = {
    CallPhase.CONFIRM,
    CallPhase.ENDING,
    # Sprint 12: a receptionist caller going quiet is a polite, spoken exit
    CallPhase.RECEPTION_INTENT,
    CallPhase.FAQ_ANSWER,
    CallPhase.TAKE_MESSAGE,
    CallPhase.AFTER_HOURS,
}

# Phases where the caller simply hanging up is a NORMAL ending ("thanks, bye"
# + click happens in conversation phases all the time). A hangup only counts
# as failed when it interrupts an in-flight action or an error state.
_BENIGN_HANGUP_PHASES = _BENIGN_TIMEOUT_PHASES | {
    CallPhase.INTENT_CAPTURE,
    CallPhase.FREEFORM,
    CallPhase.TRANSFERRING,  # the call left us for <Dial> — that is the plan
}

# Consecutive agent-brain errors tolerated before ending the call gracefully
MAX_CONSECUTIVE_ERRORS = 2

WATCHDOG_INTERVAL_S = 2.0

# T7.2: graceful shutdown — a deploy/SIGTERM drains active calls with a spoken
# ending (never dead air), bounded so shutdown can't hang on a broken call.
SHUTDOWN_DRAIN_TIMEOUT_S = 60.0
SHUTDOWN_SPEECH_TIMEOUT_S = 10.0
# Post-call reports run an LLM request; a hung provider must not block
# shutdown forever.
POSTCALL_DRAIN_TIMEOUT_S = 30.0


class VoiceChannel(BaseChannel):
    """Phone call channel — bridges Twilio voice to the Pincer agent."""

    channel_type = ChannelType.VOICE

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._handler: MessageHandler | None = None
        self._engine: VoiceEngine | None = None
        self._stream_agent: Any = None  # Sprint 5: enables the streaming turn path
        self._state_machines: dict[str, CallStateMachine] = {}
        self._response_queues: dict[str, asyncio.Queue[str]] = {}
        self._transcripts: dict[str, TranscriptLogger] = {}
        self._error_counts: dict[str, int] = {}
        self._active_turns: dict[str, asyncio.Task[None]] = {}
        self._turn_counters: dict[str, int] = {}
        self._watchdog_task: asyncio.Task[None] | None = None
        self._post_call_processor: Any = None
        self._postcall_tasks: dict[str, asyncio.Task[Any]] = {}
        # Sprint 11: one in-call tool gate per live call (tiers, approval modes)
        self._tool_gates: dict[str, InCallToolGate] = {}
        # pending hangups (farewell → grace period → end_call) and the calls
        # whose current turn is a reply to the other party's goodbye
        self._hangup_tasks: dict[str, asyncio.Task[None]] = {}
        self._farewell_turns: set[str] = set()
        # Sprint 12: inbound receptionist controller per call
        self._reception_sessions: dict[str, ReceptionSession] = {}
        self._tools: Any = None  # ToolRegistry, for the receptionist's reads/writes
        self.metrics = VoiceMetricsRegistry()

    def set_stream_agent(self, agent: Any) -> None:
        """Enable the Sprint 5 streaming pipeline: LLM tokens are cut at
        sentence boundaries and shipped to TTS while the model is still
        writing. Without it, turns fall back to the blocking handler path."""
        self._stream_agent = agent

    @property
    def name(self) -> str:
        return "voice"

    def set_engine(self, engine: VoiceEngine) -> None:
        self._engine = engine
        engine.set_on_speech(self._handle_speech)
        engine.set_on_call_end(self._handle_call_end)
        if hasattr(engine, "set_on_call_start"):
            engine.set_on_call_start(self._handle_call_start)
        engine.metrics_registry = self.metrics

    def set_tool_registry(self, tools: Any) -> None:
        """Sprint 12: the receptionist reads free/busy and writes bookings
        through the registry (via the Sprint 11 gate)."""
        self._tools = tools

    async def _handle_call_start(self, call_sid: str, state: CallState) -> None:
        """Engine registered a call: start tracking now (silence rule, receptionist greeting)."""
        try:
            self._ensure_call_tracking(call_sid, state)
        except Exception:
            logger.exception("Call-start tracking failed [%s]", call_sid)
        try:
            await self._prepare_thread_context(call_sid, state)
        except Exception:
            logger.exception("Thread context preparation failed [%s]", call_sid)
        # The briefing goes into the call's own transcript, so "what was the
        # agent told to do?" is answerable from the record the user can open,
        # not only from the logs of the process that placed the call.
        from pincer.voice.briefing import briefing_from_state, record_briefing

        record_briefing(self._transcripts.get(call_sid), briefing_from_state(state))

    async def _prepare_thread_context(self, call_sid: str, state: CallState) -> None:
        """Sprint 13 §4.3/§7: resolve this call's thread and freeze its prompt
        block onto the call state.

        Computed ONCE, at call start, for two reasons: the per-turn system
        prompt is built synchronously, and a block that cannot change mid-call
        is a bounded prompt by construction.

        For inbound calls this also performs the caller-ID match — which
        affects grouping and reporting only. What the *conversation* may know
        is decided by ``build_context`` from PINCER_THREAD_INBOUND_CONTEXT,
        and in the default `off` mode that is nothing at all.
        """
        from pincer.voice.engine import CallDirection
        from pincer.voice.threads import KIND_INBOUND_MATCHED, ThreadError, get_thread_manager

        manager = get_thread_manager(self._settings)
        direction = "inbound" if state.direction == CallDirection.INBOUND else "outbound"
        thread_id = await manager.thread_for_call(call_sid)

        if not thread_id and direction == "inbound":
            window = int(getattr(self._settings, "thread_match_window_days", 7) or 0)
            match = await manager.find_open_by_number(state.caller_number, within_days=window)
            if match is not None:
                try:
                    await manager.attach(call_sid, match.thread_id, KIND_INBOUND_MATCHED)
                    thread_id = match.thread_id
                    logger.info("Inbound call %s matched thread %s", call_sid, thread_id)
                except ThreadError as e:
                    logger.info("Inbound thread match not attached [%s]: %s", call_sid, e)

        state.metadata["thread_id"] = thread_id
        state.metadata["thread_context"] = (
            await manager.build_context(thread_id, direction, self._settings) if thread_id else ""
        )

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
        for call_sid in list(self._hangup_tasks):
            self._cancel_hangup(call_sid)
        # T7.2: graceful drain — every active call hears the spoken
        # ERROR_RECOVERY ending (in its call language) before the hangup;
        # a deploy must never be dead air mid-conversation.
        if self._engine:
            try:
                async with asyncio.timeout(SHUTDOWN_DRAIN_TIMEOUT_S):
                    for call_sid in list(self._engine.get_active_calls()):
                        await self._speak_shutdown_ending(call_sid)
                        try:
                            await self._engine.end_call(call_sid)
                        except Exception:
                            logger.exception("Error ending call %s during shutdown", call_sid)
            except TimeoutError:
                logger.error("Shutdown drain exceeded %.0fs — force-ending remaining calls", SHUTDOWN_DRAIN_TIMEOUT_S)
                for call_sid in list(self._engine.get_active_calls()):
                    with contextlib.suppress(Exception):
                        await self._engine.end_call(call_sid)
        # The drained calls' failure reports are user-facing (the initiator
        # must learn the call was cut) — let them finish before exiting,
        # bounded so a hung LLM request can't block shutdown indefinitely.
        tasks = list(self._postcall_tasks.values())
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=POSTCALL_DRAIN_TIMEOUT_S)
            for task in pending:
                logger.warning("Post-call report did not finish before shutdown — cancelling: %s", task.get_name())
                task.cancel()
        logger.info("Voice channel stopped")

    async def _speak_shutdown_ending(self, call_sid: str) -> None:
        """Polite spoken exit for a call interrupted by shutdown (Sprint 1
        rules: a forced ending is a spoken ending, never silence)."""
        from pincer.voice.prompts import get_prompt

        language, formality = self._call_language(call_sid)
        messages = get_prompt("PHASE_TIMEOUT_MESSAGES", language, formality) or {}
        goodbye = messages.get("error_recovery") or str(get_prompt("DEFAULT_TIMEOUT_MESSAGE", language, formality))

        transcript = self._transcripts.get(call_sid)
        if transcript:
            transcript.log_utterance(Speaker.AGENT, goodbye, state="shutdown")
        if self._engine:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._engine.send_speech(call_sid, goodbye), timeout=SHUTDOWN_SPEECH_TIMEOUT_S)
        sm = self._state_machines.get(call_sid)
        if sm and not sm.is_terminal:
            sm.force_terminal(CallPhase.FAILED, reason="shutdown")

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
            # Sprint 9 (T9.1): one "started" datapoint per call, and the cost
            # accumulator that per-turn LLM spend is attributed into.
            from pincer.observability.call_costs import begin_call
            from pincer.observability.metrics import record_call_started

            begin_call(call_sid)
            record_call_started(
                direction=str(state.direction),
                engine=state.engine_type,
                language=str(state.language or ""),
            )
        if call_sid not in self._transcripts:
            self._transcripts[call_sid] = TranscriptLogger(call_sid)
        if call_sid not in self._tool_gates:
            self._tool_gates[call_sid] = self._create_tool_gate(call_sid, state, sm)
        if call_sid not in self._reception_sessions:
            session = self._create_reception_session(call_sid, state, sm)
            if session is not None:
                self._reception_sessions[call_sid] = session
        self.metrics.get_or_start(call_sid, engine=state.engine_type)
        self._start_watchdog()
        return sm

    def _receptionist_call(self, state: CallState) -> bool:
        from pincer.voice.engine import CallDirection
        from pincer.voice.receptionist.profile import receptionist_active

        return state.direction == CallDirection.INBOUND and receptionist_active(self._settings)

    def _create_reception_session(
        self, call_sid: str, state: CallState, sm: CallStateMachine
    ) -> ReceptionSession | None:
        """Sprint 12: inbound + receptionist enabled → the session owns the call."""
        if not self._receptionist_call(state):
            return None
        from pincer.voice.receptionist.profile import get_profile
        from pincer.voice.receptionist.session import ReceptionSession

        profile = get_profile()
        if profile is None:
            return None
        session = ReceptionSession(
            call_sid=call_sid,
            state=state,
            sm=sm,
            settings=self._settings,
            profile=profile,
            gate=self._tool_gates.get(call_sid),
            tools=self._tools,
            transcript=self._transcripts.get(call_sid),
            engine=self._engine,
        )
        session.start()
        logger.info("Receptionist session started [%s]: open=%s", call_sid, session.is_open)
        return session

    def get_reception_session(self, call_sid: str) -> ReceptionSession | None:
        return self._reception_sessions.get(call_sid)

    def _create_tool_gate(self, call_sid: str, state: CallState, sm: CallStateMachine) -> InCallToolGate:
        """Sprint 11: per-call tool scope (§5.3) + the gate that enforces it.

        Set once at call creation: appointment calls (Sprint 6) get the fixed
        scheduling set; everything else the generic set (reads + owner-facing
        writes). The initiating user (never the callee) is the approval target
        for `user` mode.
        """
        from pincer.voice import scheduling, status_notify
        from pincer.voice.in_call_tools import InCallToolGate
        from pincer.voice.tool_policy import allowed_tools_for_call

        if self._receptionist_call(state):
            kind = "receptionist"  # Sprint 12 §9: the public line's exact, minimal tool set
        elif scheduling.get_appointment(call_sid) is not None:
            kind = "appointment"
        else:
            kind = "generic"
        allowed = allowed_tools_for_call(self._settings, kind=kind, direction=str(state.direction))
        info = status_notify.get_call_info(call_sid)
        if info is not None and info.user_id:
            target: tuple[str, str] = (info.user_id, info.channel)
        else:
            # Inbound: the OWNER (never the caller) approves `user`-mode writes
            fallback_user = state.pincer_user_id or str(getattr(self._settings, "default_user_id", "") or "")
            target = (fallback_user, "")
        logger.info("In-call tool scope [%s]: %s (%s) -> %s", call_sid, kind, state.direction, sorted(allowed))
        return InCallToolGate(
            call_sid=call_sid,
            state=state,
            sm=sm,
            settings=self._settings,
            transcript=self._transcripts.get(call_sid),
            engine=self._engine,
            approval_target=target,
            allowed_tools=allowed,
        )

    def get_tool_gate(self, call_sid: str) -> InCallToolGate | None:
        return self._tool_gates.get(call_sid)

    def get_transcript(self, call_sid: str) -> TranscriptLogger | None:
        return self._transcripts.get(call_sid)

    def _start_watchdog(self) -> None:
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._watchdog_loop(), name="voice-watchdog")

    async def _watchdog_loop(self) -> None:
        """Enforce PHASE_TIMEOUTS: a timed-out phase gets a spoken, polite exit
        and a graceful hangup — never silence, never a stuck state machine.

        Sprint 9 (T9.2) adds the stuck-call reaper: a call that outlives
        `voice_max_call_duration` + grace has escaped the phase timeouts
        entirely (that is what "stuck" means), so it is force-ended and paged.
        Every extra second costs telephony minutes and holds a line open.
        """
        try:
            while True:
                await asyncio.sleep(WATCHDOG_INTERVAL_S)
                # Sprint 12 §10.1: receptionist silence rule (re-prompt once, then hang up)
                for session in list(self._reception_sessions.values()):
                    with contextlib.suppress(Exception):
                        await session.check_silence()
                # Per-call guard: one bad call must not stop timeout
                # enforcement for every other concurrently active call.
                for call_sid, sm in list(self._state_machines.items()):
                    if sm.is_terminal or not sm.check_timeout():
                        continue
                    try:
                        await self._handle_phase_timeout(call_sid, sm)
                    except Exception:
                        logger.exception("Phase timeout handling failed [%s]", call_sid)
                try:
                    await self._reap_stuck_calls()
                except Exception:
                    logger.exception("Stuck-call reaper failed")
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Voice watchdog error")

    async def _reap_stuck_calls(self) -> None:
        """Force-end and page on calls past max_duration + grace (T9.2)."""
        if self._engine is None:
            return
        from pincer.observability.golden_signals import stuck_calls

        try:
            signal = stuck_calls(self._settings, self._engine.get_active_calls())
        except Exception:
            logger.debug("Stuck-call scan failed", exc_info=True)
            return
        if not signal.value:
            return

        for entry in signal.detail.get("stuck", []):
            call_sid = str(entry.get("call_sid", ""))
            logger.error(
                "Stuck call detected [%s]: %ds (%ds over the limit) — force-ending",
                call_sid,
                entry.get("duration_seconds"),
                entry.get("over_by_seconds"),
            )
            sm = self._state_machines.get(call_sid)
            if sm is not None and not sm.is_terminal:
                sm.force_terminal(CallPhase.FAILED, reason="stuck_max_duration_exceeded")
            with contextlib.suppress(Exception):
                await self._engine.end_call(call_sid)

        await self._page_stuck_calls(signal)

    async def _page_stuck_calls(self, signal: Any) -> None:
        try:
            from pincer.observability.alerts import Alert, Severity, deliver

            await deliver(
                self._settings,
                [
                    Alert(
                        rule="stuck_calls",
                        severity=Severity.PAGE,
                        title=f"{int(signal.value)} stuck call(s) force-ended",
                        detail=(
                            f"Exceeded {signal.detail.get('threshold_seconds')}s: "
                            + ", ".join(str(c.get("call_sid")) for c in signal.detail.get("stuck", []))
                        ),
                        value=signal.value,
                        threshold=0.0,
                        runbook="docs/operations/runbook.md#stuck-call",
                        context=signal.detail,
                    )
                ],
            )
        except Exception:
            logger.exception("Stuck-call alert delivery failed")

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

    def _build_voice_system(self, state: CallState, sm: CallStateMachine) -> str:
        """Per-turn system prompt. Delegates to the single assembly function so
        every surface that can start a call — chat tool, dashboard API,
        scheduler retry — produces the same prompt for the same call."""
        from pincer.voice.prompt_assembly import build_voice_system_prompt

        session = self._reception_sessions.get(state.call_sid)
        return build_voice_system_prompt(
            state,
            self._settings,
            sm,
            reception_block=session.system_block() if session is not None else "",
        )

    def _call_brief(self, state: CallState, language: str, formality: str) -> str:
        """The binding task block (kept as a method for callers that want just
        this part; the assembly itself lives in voice/prompt_assembly.py)."""
        from pincer.voice.prompt_assembly import build_call_briefing_block

        return build_call_briefing_block(state, self._settings, language, formality)

    # ── Ending the call ───────────────────────────────────

    def _hangup_pending(self, call_sid: str) -> bool:
        task = self._hangup_tasks.get(call_sid)
        return task is not None and not task.done()

    def _cancel_hangup(self, call_sid: str) -> None:
        task = self._hangup_tasks.pop(call_sid, None)
        if task is not None and not task.done():
            task.cancel()

    def _after_agent_turn(self, call_sid: str, sm: CallStateMachine, spoken_text: str, end_requested: bool) -> None:
        """Schedule the hangup if the reply carried [END_CALL] or answered a goodbye."""
        answered_farewell = call_sid in self._farewell_turns
        self._farewell_turns.discard(call_sid)
        if end_requested:
            self._schedule_hangup(call_sid, sm, spoken_text, "agent_end_call")
        elif answered_farewell:
            self._schedule_hangup(call_sid, sm, spoken_text, "caller_farewell")

    def _schedule_hangup(self, call_sid: str, sm: CallStateMachine, spoken_text: str, reason: str) -> None:
        """Hang up once the farewell has played plus the grace period.
        Idempotent; a real utterance during the wait cancels it."""
        if self._engine is None or sm.is_terminal or self._hangup_pending(call_sid):
            return
        try:
            grace = float(getattr(self._settings, "voice_hangup_grace_s", 2.0))
        except (TypeError, ValueError):
            grace = 2.0
        delay = call_end.estimate_speech_seconds(spoken_text) + max(0.0, grace)
        logger.info("Hangup scheduled [%s] in %.1fs (%s)", call_sid, delay, reason)
        self._hangup_tasks[call_sid] = asyncio.create_task(
            self._hangup_after(call_sid, sm, delay, reason), name=f"voice-hangup-{call_sid}"
        )

    async def _hangup_after(self, call_sid: str, sm: CallStateMachine, delay: float, reason: str) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        self._hangup_tasks.pop(call_sid, None)
        if self._engine is None or self._engine.get_call_state(call_sid) is None:
            return  # already gone (other side hung up first)
        if not sm.is_terminal:
            sm.force_terminal(CallPhase.COMPLETED, reason=reason)
        with contextlib.suppress(Exception):
            await self._engine.end_call(call_sid)

    def _time_context(self, language: str, formality: str) -> str:
        from pincer.voice.prompt_assembly import build_time_context

        return build_time_context(self._settings, language, formality)

    async def _handle_speech(self, call_sid: str, text: str) -> None:
        """Called when the caller speaks (STT output or ConversationRelay text).

        Wraps the turn in the Sprint 9 cost context: every LLM call made while
        this turn runs — including the streaming sub-task, which inherits the
        context at creation — is billed to this call_sid. Outside a turn the
        binding is empty, which is what keeps a user's ordinary chat traffic
        out of the call's cost record.
        """
        from pincer.observability.call_costs import call_context

        with call_context(call_sid):
            await self._handle_speech_turn(call_sid, text)

    async def _handle_speech_turn(self, call_sid: str, text: str) -> None:
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
        # The caller spoke — reset the phase-inactivity clock so the watchdog
        # only ends calls that actually went silent, never active ones.
        sm.touch()

        transcript = self._transcripts[call_sid]
        transcript.log_utterance(Speaker.CALLER, text, state=str(sm.phase))

        # The greeting phases end the moment the callee speaks
        if sm.phase in (CallPhase.GREETING, CallPhase.OUTBOUND_GREETING):
            sm.transition(CallPhase.INTENT_CAPTURE, "caller_spoke")

        # Goodbyes (the receptionist line has its own endings). After the agent
        # already said goodbye a farewell just ends the call; said first, the
        # next turn is the agent's goodbye. Anything else cancels a pending hangup.
        self._farewell_turns.discard(call_sid)
        if self._reception_sessions.get(call_sid) is None:
            call_lang = str(state.language or "en")[:2]
            if call_end.is_farewell(text, call_lang):
                if self._hangup_pending(call_sid) or call_end.last_agent_said_farewell(transcript, call_lang):
                    logger.info("Mutual goodbye [%s] — hanging up", call_sid)
                    self._schedule_hangup(call_sid, sm, "", "mutual_goodbye")
                    return
                self._farewell_turns.add(call_sid)
            elif self._hangup_pending(call_sid):
                logger.info("Caller continued after goodbye [%s] — hangup cancelled", call_sid)
                self._cancel_hangup(call_sid)

        # Sprint 11: a pending verbal confirmation is parsed BEFORE the LLM
        # turn — yes/no is decided deterministically, never by the model.
        from pincer.voice.in_call_tools import bind_gate

        gate = self._tool_gates.get(call_sid)
        verdict_note = ""
        if gate is not None:
            gate.begin_turn()
            verdict = await gate.handle_caller_utterance(text)
            if verdict.handled:
                return  # the gate re-asked; no LLM turn
            verdict_note = verdict.system_note

        # Sprint 12: the receptionist session handles slot filling, booking,
        # transfer, and deflections deterministically; only intent capture and
        # FAQ answers go to the LLM (with the constrained instruction).
        session = self._reception_sessions.get(call_sid)
        if session is not None:
            plan = await session.on_caller_utterance(text)
            if plan.handled:
                return
            if plan.system_note:
                verdict_note = f"{verdict_note}\n\n{plan.system_note}" if verdict_note else plan.system_note
            if plan.override_text:
                text = plan.override_text

        # Barge-in at the turn level (Sprint 5): a new caller utterance while
        # the previous streamed turn is still generating/speaking cancels that
        # turn — no queued sentences may play after the interrupt.
        prior_turn = self._active_turns.pop(call_sid, None)
        if prior_turn is not None and not prior_turn.done():
            prior_turn.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await prior_turn
            if self._engine:
                with contextlib.suppress(Exception):
                    await self._engine.interrupt_speech(call_sid)
            logger.info("Streamed turn cancelled by barge-in [%s]", call_sid)

        extra_system = self._build_voice_system(state, sm)
        if verdict_note:
            extra_system = f"{extra_system}\n\n{verdict_note}"
        if call_sid in self._farewell_turns:
            from pincer.voice.prompts import get_prompt

            language, formality = self._call_language(call_sid)
            note = str(get_prompt("CALLER_FAREWELL_NOTE", language, formality) or "")
            if note:
                extra_system = f"{extra_system}\n\n{note}"

        # Sprint 5 streaming path: sentence-by-sentence LLM→TTS pipeline
        if self._stream_agent is not None and self._engine is not None:
            with bind_gate(gate):
                turn = asyncio.create_task(
                    self._run_streaming_turn(call_sid, state, sm, transcript, metrics, text, extra_system),
                    name=f"voice-turn-{call_sid}",
                )
            self._active_turns[call_sid] = turn
            try:
                await turn
            except asyncio.CancelledError:
                pass  # barge-in — the interrupting utterance owns the call now
            finally:
                if self._active_turns.get(call_sid) is turn:
                    self._active_turns.pop(call_sid, None)
            return

        incoming = IncomingMessage(
            user_id=state.caller_number,
            channel="voice",
            text=text,
            channel_type=ChannelType.VOICE,
            extra_system=extra_system,
        )

        async def _regenerate(note: str) -> str | None:
            """One-shot drift correction: the corrective note goes through the
            normal handler so the repair is part of the session history."""
            if not self._handler:
                return None
            retry = IncomingMessage(
                user_id=state.caller_number,
                channel="voice",
                text=note,
                channel_type=ChannelType.VOICE,
                extra_system=extra_system,
            )
            return await self._handler(retry)

        try:
            with bind_gate(gate):
                response = await self._handler(incoming)
            self._error_counts.pop(call_sid, None)
            if gate is not None and gate.suppress_llm_speech:
                # The gate already spoke the outcome (verify question, hold,
                # declined, deferred, error) — the model's wording is dropped.
                response = ""
            if response and session is not None:
                # Sprint 12: [INTENT:…] token parsed/stripped; actionable intents
                # are spoken by the session, the model's wording is dropped.
                response, handled_by_session = await session.process_response(response)
                if handled_by_session:
                    response = ""
            if response and self._engine:
                from pincer.voice.language_guard import check_and_fix
                from pincer.voice.scheduling import process_appointment_response

                response, _switched = await check_and_fix(
                    response,
                    state,
                    engine=self._engine,
                    settings=self._settings,
                    transcript=transcript,
                    regenerate=_regenerate,
                )
                # Appointment token (Sprint 6): validated against the slot
                # whitelist; out-of-slot confirmations are never spoken.
                response = process_appointment_response(response, state, self._settings, transcript)
            end_requested, response = call_end.parse_end_call_token(response or "")
            if response and self._engine:
                metrics.mark_agent_speech_start()
                delivered = await self._engine.send_speech(call_sid, response)
                # Honest transcripts: "text generated" is not "audio heard".
                # An undelivered agent turn is marked so post-call summaries
                # and the truthfulness check reflect what the caller heard.
                transcript.log_utterance(
                    Speaker.AGENT,
                    response,
                    state=str(sm.phase) if delivered else "undelivered",
                )
            self._after_agent_turn(call_sid, sm, response or "", end_requested)
        except Exception:
            logger.exception("Error handling voice input for call %s", call_sid)
            await self._handle_brain_error(call_sid, sm)

    # ── Streaming turn (Sprint 5, T5.1/T5.2/T5.7) ─────────

    async def _run_streaming_turn(
        self,
        call_sid: str,
        state: CallState,
        sm: CallStateMachine,
        transcript: TranscriptLogger,
        metrics: Any,
        text: str,
        extra_system: str,
    ) -> None:
        """One caller turn on the streaming pipeline: LLM tokens → sentence
        boundaries → TTS immediately, so the first sentence plays while the
        model writes the next (stage D leaves the critical path).

        Guard interaction (T5.2): the FIRST sentence is gated — switch and
        appointment tokens are parsed/validated, and a clear language mismatch
        flips the turn into buffered mode (nothing spoken; the whole turn is
        post-processed by the existing check_and_fix regeneration). Subsequent
        sentences stream unchecked within the turn.
        """
        import time

        from pincer.core.agent import StreamEventType
        from pincer.voice.language_guard import detect_language, parse_switch_token, perform_switch
        from pincer.voice.prompts import get_prompt
        from pincer.voice.scheduling import get_appointment, process_appointment_response
        from pincer.voice.sentence_stream import SentenceAssembler, strip_tts_markup
        from pincer.voice.voice_tools import get_filler_phrase

        assert self._engine is not None and self._stream_agent is not None
        engine = self._engine
        gate = self._tool_gates.get(call_sid)

        turn_no = self._turn_counters.get(call_sid, 0) + 1
        self._turn_counters[call_sid] = turn_no
        t0 = time.monotonic()
        stamps: dict[str, float] = {}

        def stamp(name: str) -> None:
            stamps.setdefault(name, (time.monotonic() - t0) * 1000.0)

        assembler = SentenceAssembler()
        spoken_any = False
        open_stream = False  # a last=False token was sent (CR utterance open)
        first_gated = False
        buffered_mode = False  # drift detected: collect silently, post-process whole turn
        suppress_rest = False  # decline/deferral already spoken: swallow the rest
        full_text = ""
        end_requested = False  # [END_CALL] seen in the reply
        spoken_text = ""  # what was actually sent to TTS this turn
        canonical_id = state.pincer_user_id or state.caller_number

        # Only ConversationRelay buffers partial tokens and needs an explicit
        # last=True closer; per-utterance engines (Media Streams, harness)
        # synthesize each sentence independently.
        buffering_engine = getattr(engine, "engine_name", "") == "conversation_relay"

        async def speak(sentence: str, *, last: bool) -> None:
            nonlocal spoken_any, open_stream, end_requested, spoken_text
            if sentence:
                token, sentence = call_end.parse_end_call_token(sentence)
                end_requested = end_requested or token
            sentence = strip_tts_markup(sentence) if sentence else sentence
            if not sentence and not (last and open_stream and buffering_engine):
                return
            if not spoken_any and sentence:
                stamp("first_dispatch_ms")
                metrics.mark_agent_speech_start()
            delivered = await engine.send_speech(call_sid, sentence, last=last)
            if delivered and buffering_engine:
                open_stream = not last
            if sentence:
                spoken_any = True
                spoken_text = f"{spoken_text} {sentence}".strip()
                transcript.log_utterance(
                    Speaker.AGENT, sentence, state=str(sm.phase) if delivered else "undelivered"
                )

        async def gate_first_sentence(sentence: str) -> str:
            """Token + drift gate for the first sentence (local, ~0ms)."""
            nonlocal buffered_mode, suppress_rest
            call_lang = str(state.language or "en")[:2]
            requested, stripped = parse_switch_token(sentence)
            if requested is not None:
                from pincer.voice.language import de_formality, supported_languages

                if requested == call_lang:
                    sentence = stripped
                elif requested in supported_languages(self._settings):
                    await perform_switch(
                        state, requested, engine=engine, settings=self._settings, transcript=transcript
                    )
                    sentence = stripped or str(
                        get_prompt("LANGUAGE_SWITCH_ACK", requested, de_formality(self._settings))
                    )
                else:
                    suppress_rest = True
                    return str(get_prompt("LANGUAGE_SWITCH_UNSUPPORTED", call_lang, de_formality(self._settings)))

            session = self._reception_sessions.get(call_sid)
            if session is not None:
                # Sprint 12: intent token on the first sentence; the session
                # may take over the turn (message/booking/transfer/unknown).
                sentence, session_spoke = await session.process_response(sentence)
                if session_spoke:
                    suppress_rest = True
                    return ""
                if not sentence:
                    return ""

            task = get_appointment(call_sid)
            before_status = task.status if task else ""
            sentence = process_appointment_response(sentence, state, self._settings, transcript)
            task = get_appointment(call_sid)
            if task and task.status == "out_of_candidates" and before_status != "out_of_candidates":
                suppress_rest = True  # the deferral replaces the whole turn
                return sentence

            detected = detect_language(sentence)
            if detected is not None and detected != str(state.language or "en")[:2]:
                logger.warning(
                    "Language drift on streamed first sentence [%s] (%s != %s) — buffering turn",
                    call_sid,
                    detected,
                    state.language,
                )
                buffered_mode = True
                return ""
            return sentence

        if gate is not None:
            # The gate's deterministic lines go through this turn's speech
            # path (transcript + CR stream bookkeeping stay consistent).
            async def _gate_speak(text: str) -> None:
                await speak(text, last=True)

            gate.set_speaker(_gate_speak, lambda: spoken_any)

        agent_timings: dict[str, Any] = {}
        try:
            async for chunk in self._stream_agent.stream_voice_turn(
                user_id=canonical_id,
                channel="voice",
                text=text,
                extra_system=extra_system,
                channel_user_id=state.caller_number,
                timings=agent_timings,
            ):
                if chunk.type == StreamEventType.TEXT:
                    stamp("llm_first_token_ms")
                    if buffered_mode or suppress_rest:
                        continue  # DONE carries the full text
                    for sentence in assembler.feed(chunk.content):
                        stamp("first_sentence_ms")
                        if not first_gated:
                            first_gated = True
                            sentence = await gate_first_sentence(sentence)
                            if buffered_mode:
                                break
                            if suppress_rest:
                                await speak(sentence, last=True)
                                break
                        if sentence:
                            await speak(sentence, last=False)
                elif chunk.type == StreamEventType.TOOL_START:
                    # T5.7: instant acknowledgment — the caller hears a filler
                    # the moment a tool starts, never dead air while it runs.
                    # With an in-call gate (Sprint 11) the gate owns the
                    # filler (§6.1: only for slow tools) and every other line.
                    if gate is None and not spoken_any:
                        filler = get_filler_phrase(
                            str(getattr(self._settings, "voice_filler_phrases", "") or ""),
                            language=state.language,
                        )
                        await speak(filler, last=True)
                elif chunk.type == StreamEventType.TOOL_DONE:
                    if gate is not None and gate.suppress_llm_speech:
                        suppress_rest = True  # the gate spoke the outcome
                elif chunk.type == StreamEventType.DONE:
                    stamp("llm_done_ms")
                    full_text = chunk.content
            self._error_counts.pop(call_sid, None)
        except asyncio.CancelledError:
            # Barge-in: close any open CR utterance and stop cleanly.
            if open_stream:
                with contextlib.suppress(Exception):
                    await engine.send_speech(call_sid, "", last=True)
            raise
        except Exception:
            logger.exception("Streaming turn failed [%s]", call_sid)
            if open_stream:
                with contextlib.suppress(Exception):
                    await engine.send_speech(call_sid, "", last=True)
            await self._handle_brain_error(call_sid, sm)
            self._log_turn_latency(call_sid, state, turn_no, t0, {**agent_timings, **stamps}, streamed=True, error=True)
            return
        finally:
            if gate is not None:
                gate.set_speaker(None)

        if buffered_mode:
            # Rare drift path: nothing was spoken; run the whole-turn guard
            # (regeneration included) exactly like the blocking pipeline.
            await self._speak_guarded_block(call_sid, state, sm, transcript, metrics, full_text, extra_system)
        elif not suppress_rest:
            remainder = assembler.flush()
            if remainder and not first_gated:
                # Single short utterance without terminator: still gate it
                first_gated = True
                remainder = await gate_first_sentence(remainder)
                if buffered_mode:
                    await self._speak_guarded_block(call_sid, state, sm, transcript, metrics, full_text, extra_system)
                    remainder = ""
            if remainder:
                await speak(remainder, last=True)
            elif open_stream:
                await speak("", last=True)  # close the CR utterance

        if not buffered_mode:  # the guarded block handles its own turn
            self._after_agent_turn(call_sid, sm, spoken_text, end_requested)
        self._log_turn_latency(call_sid, state, turn_no, t0, {**agent_timings, **stamps}, streamed=True)

    async def _speak_guarded_block(
        self,
        call_sid: str,
        state: CallState,
        sm: CallStateMachine,
        transcript: TranscriptLogger,
        metrics: Any,
        full_text: str,
        extra_system: str,
    ) -> None:
        """Fallback for the buffered drift path: apply the full non-streaming
        guard (with one regeneration) and speak the result as one utterance."""
        from pincer.voice.language_guard import check_and_fix
        from pincer.voice.scheduling import process_appointment_response

        if not full_text or self._engine is None:
            return

        async def _regenerate(note: str) -> str | None:
            if not self._handler:
                return None
            retry = IncomingMessage(
                user_id=state.caller_number,
                channel="voice",
                text=note,
                channel_type=ChannelType.VOICE,
                extra_system=extra_system,
            )
            return await self._handler(retry)

        response, _switched = await check_and_fix(
            full_text,
            state,
            engine=self._engine,
            settings=self._settings,
            transcript=transcript,
            regenerate=_regenerate,
        )
        response = process_appointment_response(response, state, self._settings, transcript)
        from pincer.voice.sentence_stream import strip_tts_markup as _strip

        response = _strip(response) if response else response
        end_requested, response = call_end.parse_end_call_token(response or "")
        if response and self._engine:
            metrics.mark_agent_speech_start()
            delivered = await self._engine.send_speech(call_sid, response)
            transcript.log_utterance(
                Speaker.AGENT, response, state=str(sm.phase) if delivered else "undelivered"
            )
        self._after_agent_turn(call_sid, sm, response or "", end_requested)

    def _log_turn_latency(
        self,
        call_sid: str,
        state: CallState,
        turn_no: int,
        t0: float,
        stamps: dict[str, float],
        *,
        streamed: bool,
        error: bool = False,
    ) -> None:
        """T5.1: one structured line per turn + a JSONL record for the
        `pincer voice latency-report` CLI. Never raises."""
        import json
        import time
        from datetime import UTC, datetime

        record: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "call_sid": call_sid,
            "turn": turn_no,
            "engine": state.engine_type,
            "language": state.language,
            "streamed": streamed,
            "error": error,
            "total_ms": round((time.monotonic() - t0) * 1000.0, 1),
            **{k: (round(v, 1) if isinstance(v, int | float) else v) for k, v in stamps.items()},
        }
        logger.info(
            "TURN_LATENCY call=%s turn=%d llm_first_token=%sms first_sentence=%sms "
            "first_dispatch=%sms llm_done=%sms total=%sms",
            call_sid,
            turn_no,
            record.get("llm_first_token_ms", "-"),
            record.get("first_sentence_ms", "-"),
            record.get("first_dispatch_ms", "-"),
            record.get("llm_done_ms", "-"),
            record["total_ms"],
        )
        # T9.1: the same numbers as histograms, so p50/p95 are queryable in the
        # metrics backend instead of only greppable out of a JSONL file.
        from pincer.observability.metrics import record_turn_latency

        record_turn_latency(
            total_s=record["total_ms"] / 1000.0,
            engine=state.engine_type,
            language=str(state.language or ""),
            streamed=streamed,
            error=error,
            stages_ms={k: v for k, v in stamps.items() if isinstance(v, int | float)},
        )

        try:
            log_dir = getattr(self._settings, "data_dir", None)
            if log_dir:
                path = log_dir / "logs" / "voice_latency.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            logger.debug("voice_latency.jsonl write failed", exc_info=True)

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
        # Sprint 15: the listen-in fork normally ends itself (Twilio stops the
        # stream when the call ends); this is the belt-and-braces close so no
        # dashboard listener can outlive the call.
        with contextlib.suppress(Exception):
            from pincer.voice.monitor import get_monitor_hub

            get_monitor_hub().end(call_sid)
        self._error_counts.pop(call_sid, None)
        self._cancel_hangup(call_sid)
        self._farewell_turns.discard(call_sid)
        # Sprint 11: a hangup while a `user` approval card is open cancels the
        # card ("call ended") — nothing executes after the callee is gone.
        gate = self._tool_gates.pop(call_sid, None)
        if gate is not None:
            await gate.on_call_end()
        session = self._reception_sessions.pop(call_sid, None)
        if session is not None:
            with contextlib.suppress(Exception):
                await session.on_call_end()

        completed = True
        if sm is not None:
            if not sm.is_terminal:
                # Hangup before a clean ENDING: in conversation phases that's a
                # NORMAL ending ("thanks, bye" + click) — only a hangup during
                # an action (VERIFY/EXECUTE), an error state, or before any
                # conversation (greeting phases) counts as failed.
                terminal = CallPhase.COMPLETED if sm.phase in _BENIGN_HANGUP_PHASES else CallPhase.FAILED
                sm.force_terminal(terminal, reason="call_end_cleanup")
            completed = sm.phase == CallPhase.COMPLETED

        unverified: list[str] = []
        if transcript:
            unverified = transcript.verify_completion_claims()

        summary = self.metrics.finish_call(call_sid)

        # Sprint 9 (T9.3): one stable failure code per terminated call, derived
        # from the state machine's own terminal reason so the code in the
        # database, the metric label, and the digest are always the same value.
        failure_code = self._classify_call_failure(sm, transcript, completed)
        state.metadata["failure_code"] = str(failure_code)

        logger.info(
            "Call ended: %s (%s, %ds, %s, failure_code=%s)",
            call_sid,
            state.direction,
            state.duration_seconds,
            "completed" if completed else "failed",
            failure_code,
        )

        await self._record_call_telemetry(call_sid, state, failure_code, completed, summary)

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
        end_reason = str(state.metadata.get("end_reason") or "")
        if end_reason:
            outcome = f"{end_reason} ({state.duration_seconds}s)"
        if unverified:
            outcome += " — note: the agent made a completion claim I could not verify against tool results"
        with contextlib.suppress(Exception):
            await notify_ended(call_sid, outcome)

    def _classify_call_failure(
        self,
        sm: CallStateMachine | None,
        transcript: TranscriptLogger | None,
        completed: bool,
    ) -> Any:
        """Map this call's terminal reason onto the stable taxonomy (T9.3).

        A completed call whose agent turns never reached the caller is NOT a
        success: that is the Hotfix-3 "silent agent" shape, where transcripts
        look healthy and the callee heard nothing. It is reclassified as
        `no_audio` so the failure is visible instead of scoring as a win.
        """
        from pincer.observability.failure_codes import FailureCode, classify_failure

        if transcript is not None and self._call_was_silent(transcript):
            return FailureCode.NO_AUDIO
        if completed:
            return FailureCode.NONE

        reason = ""
        if sm is not None:
            transitions = sm.state.transitions
            if transitions:
                reason = str(transitions[-1].reason or "")
        return classify_failure(reason, completed=False)

    @staticmethod
    def _call_was_silent(transcript: TranscriptLogger) -> bool:
        """True when the agent produced turns but every one was undelivered."""
        agent_entries = [e for e in transcript.entries if e.speaker == Speaker.AGENT and e.is_final]
        if not agent_entries:
            return False
        return all(e.state == "undelivered" for e in agent_entries)

    async def _record_call_telemetry(
        self,
        call_sid: str,
        state: CallState,
        failure_code: Any,
        completed: bool,
        summary: dict[str, Any] | None,
    ) -> None:
        """Emit the call-ended metric and persist the priced cost record (T9.1).

        Best effort by construction: a metrics or pricing failure must never
        interfere with the post-call pipeline the user is waiting on.
        """
        try:
            from pincer.observability.call_costs import price_call, save_call_cost
            from pincer.observability.metrics import record_call_ended

            record_call_ended(
                direction=str(state.direction),
                outcome="completed" if completed else "failed",
                failure_code=str(failure_code),
                engine=state.engine_type,
                language=str(state.language or ""),
                duration_s=float(state.duration_seconds),
            )
            cost = price_call(
                self._settings,
                call_sid=call_sid,
                direction=str(state.direction),
                engine=state.engine_type,
                language=str(state.language or ""),
                duration_seconds=int(state.duration_seconds),
                tts_characters=int((summary or {}).get("tts_characters") or 0),
            )
            await save_call_cost(self._settings, cost)
        except Exception:
            logger.exception("Call telemetry/cost recording failed for %s", call_sid)

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
