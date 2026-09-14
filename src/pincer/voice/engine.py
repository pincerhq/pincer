"""
Voice engine abstraction layer.

Provides a common interface for both ConversationRelay (Phase 1)
and Media Streams (Phase 2) Twilio integrations.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from pincer.config import Settings

logger = logging.getLogger(__name__)


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _looks_like_base64(s: str) -> bool:
    """Heuristic: base64 strings are longer and use A-Za-z0-9+/=."""
    if len(s) < 20:
        return False
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
    return all(c in allowed or c.isspace() for c in s[:100])


class CallDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


# An answering machine's outgoing greeting is transcribed like any speech, so
# text alone can't distinguish "human replied to us" from "machine talked at
# us". Twilio's sync AMD verdict lands within the first seconds of the call;
# only speech after this window counts as a demonstrably conversing human.
AMD_GRACE_SECONDS = 10.0

# How long a torn-down call stays recognisable to late status webhooks. Twilio
# retries a status callback for minutes, so this outlives the post-call
# pipeline rather than just the create_task hand-off.
_ENDED_CALL_TTL_S = 900.0
_MAX_ENDED_CALLS = 500


@dataclass
class CallState:
    """Runtime state for an active voice call."""

    call_sid: str
    direction: CallDirection
    caller_number: str
    target_number: str = ""
    target_name: str = ""
    purpose: str = ""
    instructions: str = ""  # extra guidance from the user, shown to the agent on the call
    language: str = "en"
    engine_type: str = "conversation_relay"
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = None
    user_id: str = ""
    pincer_user_id: str = ""
    session_id: str = ""
    recording_consent: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> int:
        end = self.ended_at or datetime.now(UTC)
        return int((end - self.started_at).total_seconds())

    def mark_caller_spoke(self) -> None:
        """Flag a demonstrably conversing counterpart — shields the call from
        late/false AMD "machine" verdicts in the status webhook. Speech inside
        the AMD grace window does not count: a voicemail greeting is
        transcribed too, and it must not suppress the AMD hangup."""
        if (datetime.now(UTC) - self.started_at).total_seconds() > AMD_GRACE_SECONDS:
            self.metadata["caller_spoke"] = True


@dataclass
class _SpeechProgress:
    """How much of one utterance has reached Twilio.

    Shared across the synthesis attempt and its retry so the retry resumes
    instead of repeating. Segment-level rather than byte-level: neural TTS is
    not byte-reproducible, so re-synthesizing and skipping N bytes would cut
    mid-phoneme rather than resume cleanly.
    """

    segments_done: int = 0
    wrote_audio: bool = False
    first_chunk_ms: float | None = None
    #: Failed synthesis attempts so far. Spans are keyed on it so a resumed
    #: retry shows up as its own bar rather than silently extending the first.
    attempts: int = 0


class VoiceEngine(ABC):
    """Abstract interface for voice call handling.

    Both ConversationRelay and Media Streams implement this so the
    agent brain, state machine, and compliance layers are engine-agnostic.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._active_calls: dict[str, CallState] = {}
        self._on_speech_callback: Callable | None = None
        self._on_call_end_callback: Callable | None = None
        # Sprint 12: lets the channel start per-call tracking (silence rule,
        # receptionist session) the moment a call is registered, not on the
        # first caller utterance.
        self._on_call_start_callback: Callable | None = None
        # Outbound calls briefed and registered BEFORE the Twilio REST dial,
        # keyed by a temporary id until the real CallSid comes back. See
        # register_pending_outbound().
        self._pending_outbound: dict[str, CallState] = {}
        # call_sid -> monotonic time of teardown. See was_recently_ended().
        self._ended_calls: dict[str, float] = {}
        # Optional VoiceMetricsRegistry, injected by VoiceChannel.set_engine
        self.metrics_registry: Any = None

    def set_on_speech(self, callback: Callable) -> None:
        self._on_speech_callback = callback

    def set_on_call_end(self, callback: Callable) -> None:
        self._on_call_end_callback = callback

    def set_on_call_start(self, callback: Callable) -> None:
        self._on_call_start_callback = callback

    @abstractmethod
    async def on_call_start(
        self,
        call_sid: str,
        caller: str,
        direction: CallDirection,
        target_number: str = "",
        target_name: str = "",
        purpose: str = "",
        language: str = "",
        instructions: str = "",
    ) -> CallState: ...

    @abstractmethod
    async def on_speech_input(self, call_sid: str, text_or_audio: Any) -> None: ...

    @abstractmethod
    async def send_speech(self, call_sid: str, text_or_audio: Any, *, last: bool = True) -> bool:
        """Speak on the call. Returns True only when the audio was actually
        handed to the transport — a False return means the caller heard
        nothing (transcripts must record that honestly).

        ``last=False`` marks a partial utterance in a streamed turn (Sprint 5):
        ConversationRelay buffers tokens until a ``last=True`` token closes the
        reply; engines with per-utterance synthesis ignore the flag."""
        ...

    @abstractmethod
    async def interrupt_speech(self, call_sid: str) -> None: ...

    @abstractmethod
    async def transfer_call(
        self,
        call_sid: str,
        target_number: str,
        *,
        timeout_s: int = 30,
        action_url: str = "",
        announce: str = "",
        language: str = "",
    ) -> None: ...

    @abstractmethod
    async def end_call(self, call_sid: str) -> None: ...

    @abstractmethod
    async def send_dtmf(self, call_sid: str, digits: str) -> None: ...

    def get_call_state(self, call_sid: str) -> CallState | None:
        return self._active_calls.get(call_sid)

    @abstractmethod
    async def close_media_stream(self, call_sid: str) -> None:
        """Override in MediaStreamEngine to close STT stream and consumer."""

    async def on_media_closed(self, call_sid: str) -> None:
        """The media WebSocket closed: the call is over on Twilio's side.

        Inbound calls get no status callback unless one is configured in the
        console, so this is their end signal. No-op if we already ended the
        call or it is being transferred; never issues a REST hangup.
        """
        state = self._active_calls.get(call_sid)
        if state is None or state.metadata.get("transferring"):
            return
        logger.info("Media session closed by Twilio [%s] — ending call", call_sid)
        await self.close_media_stream(call_sid)
        ended = await self._unregister_call(call_sid)
        if ended and self._on_call_end_callback:
            await self._on_call_end_callback(call_sid, ended)

    def get_active_calls(self) -> dict[str, CallState]:
        return dict(self._active_calls)

    async def fallback_and_end(self, call_sid: str) -> None:
        """TTS is broken mid-call: apologize via Twilio's own <Say> (basic TTS,
        always available) and end the call cleanly — never dead air. Shared by
        both engines (Media Streams synthesis failure, ConversationRelay
        repeated 64111 token-to-speech errors)."""
        state = self._active_calls.get(call_sid)
        if not state:
            return

        from pincer.voice.language import de_formality, relay_language
        from pincer.voice.prompts import get_prompt

        apology = get_prompt("TTS_FAILURE_GOODBYE", state.language, de_formality(self._settings))
        try:
            from twilio.rest import Client

            client = Client(
                self._settings.twilio_account_sid,
                self._settings.twilio_auth_token.get_secret_value(),
            )
            twiml = f'<Response><Say language="{relay_language(state.language)}">{apology}</Say><Hangup/></Response>'
            client.calls(call_sid).update(twiml=twiml)
        except Exception:
            logger.exception("TTS fallback <Say> failed [%s] — ending call directly", call_sid)
            with contextlib.suppress(Exception):
                await self.end_call(call_sid)
            return

        # Twilio speaks the apology and hangs up; clean up our side now.
        await self.close_media_stream(call_sid)
        ended = await self._unregister_call(call_sid)
        if ended and self._on_call_end_callback:
            await self._on_call_end_callback(call_sid, ended)

    # ── Outbound registration, before the dial ────────────
    #
    # The briefing used to be registered AFTER `client.calls.create()`
    # returned. Twilio can connect the ConversationRelay socket before that
    # call even returns, and the setup handler that then found no state
    # invented a fresh INBOUND one — an unbriefed generic persona on a call
    # the user had given a task. The briefed state was orphaned, and nothing
    # anywhere reported a problem.
    #
    # So the state is now built first, parked under a temporary key, and
    # re-keyed to the real CallSid when the REST call returns. A setup that
    # arrives in between waits for the promotion (await_call_state) instead of
    # improvising.

    async def register_pending_outbound(
        self,
        briefing: Any,
        target_number: str,
        language: str = "",
        instructions: str = "",
    ) -> CallState:
        """Register a briefed outbound call before it is dialled.

        Returns a CallState under a temporary ``pending:<uuid>`` key. It is NOT
        in `_active_calls` and the on_call_start callback has NOT fired: the
        per-call machinery (state machine, transcript, thread context) is keyed
        by CallSid, and starting it under a temporary id would produce tracking
        for a call that may never exist.
        """
        import uuid

        from pincer.voice.language import resolve_call_language

        pending_sid = f"pending:{uuid.uuid4().hex}"
        state = CallState(
            call_sid=pending_sid,
            direction=CallDirection.OUTBOUND,
            caller_number=target_number,
            target_number=target_number,
            target_name=str(getattr(briefing, "target_name", "") or ""),
            purpose=str(getattr(briefing, "task", "") or ""),
            instructions=instructions or str(getattr(briefing, "instructions", "") or ""),
            language=resolve_call_language(self._settings, language),
            engine_type=self.engine_name,
        )
        state.metadata["briefing"] = briefing
        self._pending_outbound[pending_sid] = state
        logger.info("Outbound call pre-registered %s -> %s", pending_sid, target_number)
        return state

    async def promote_pending(self, pre_state: CallState, call_sid: str) -> CallState | None:
        """Re-key a pre-registered outbound call to its real CallSid.

        Publishing the state is all this does — a relay `setup` racing the
        promotion then finds the briefed call instead of inventing an
        unbriefed one. The call is still RINGING at this point, so no clock
        starts here; see mark_call_answered(). Idempotent: a second promotion
        of the same pending state is a no-op that returns the live call.
        """
        if not call_sid:
            return None
        pending = self._pending_outbound.pop(pre_state.call_sid, None)
        state = pending or self._active_calls.get(call_sid)
        if state is None:
            return self._active_calls.get(call_sid)
        state.call_sid = call_sid
        self._active_calls[call_sid] = state
        logger.info("Outbound call promoted to %s (language=%s)", call_sid, state.language)
        # NOTE: the on_call_start callback is deliberately NOT fired here.
        # Promotion happens the moment the REST dial returns — the phone is
        # still ringing. Starting per-call tracking now would start the
        # conversation phase clock during the ring, and the watchdog would
        # speak its timeout goodbye and hang up before the callee had even
        # answered. Tracking starts in mark_call_answered().
        return state

    async def mark_call_answered(self, call_sid: str) -> CallState | None:
        """The callee picked up: start per-call tracking, once.

        This is the moment every clock should start from — the state machine's
        phase timeouts, the talk-time accumulator, the call-started metric. For
        a pre-registered outbound call the state has existed since before the
        dial, so "registered" and "answered" are different events and only this
        one means a conversation is happening.

        Idempotent: a `setup` that arrives again (coming back from a <Dial>
        transfer, or a reconnect) must not restart the clocks.
        """
        state = self._active_calls.get(call_sid)
        if state is None or state.metadata.get("answered_at") is not None:
            return state

        answered = datetime.now(UTC)
        state.metadata["answered_at"] = answered
        from pincer.voice.telemetry import hooks as telemetry

        telemetry.call_answered(call_sid, engine=self.engine_name, direction=str(state.direction))
        # An outbound state exists from before client.calls.create(), so
        # started_at has been running through the whole ring. Re-anchor it to
        # the pickup, keeping the dial time for diagnostics. Both things that
        # read started_at break otherwise: duration_seconds bills and reports
        # the ring as conversation, and the AMD grace window in
        # mark_caller_spoke() is already spent at pickup on any call that rang
        # longer than AMD_GRACE_SECONDS — so a voicemail greeting sets
        # caller_spoke and disarms the AMD shield in _handle_amd_verdict.
        state.metadata.setdefault("dialed_at", state.started_at)
        state.started_at = answered
        # Ringing is not silence: the talk-time clock is re-anchored here so
        # the wait for an answer does not land in the call's silence figure.
        from pincer.voice.analytics import TalkTimeAccumulator

        state.metadata["talktime"] = TalkTimeAccumulator(method=self.analytics_method)

        if self._on_call_start_callback is not None:
            try:
                await self._on_call_start_callback(call_sid, state)
            except Exception:
                logger.exception("on_call_start callback failed [%s]", call_sid)
        return state

    def discard_pending(self, pre_state: CallState) -> None:
        """Drop a pre-registration whose dial never happened (Twilio raised)."""
        if self._pending_outbound.pop(pre_state.call_sid, None) is not None:
            logger.info("Outbound pre-registration discarded: %s", pre_state.call_sid)

    async def await_call_state(self, call_sid: str, timeout: float = 3.0, interval: float = 0.1) -> CallState | None:
        """Wait up to `timeout` for a call's state to be registered.

        For the setup-before-registration race: the dial is in flight and the
        promotion is microseconds away, so a short poll turns a lost briefing
        into a slightly later one.
        """
        state = self._active_calls.get(call_sid)
        if state is not None or timeout <= 0:
            return state
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            await asyncio.sleep(interval)
            state = self._active_calls.get(call_sid)
            if state is not None:
                logger.info("Outbound state for %s arrived after the relay setup", call_sid)
                return state
        return None

    async def _register_call(
        self,
        call_sid: str,
        caller: str,
        direction: CallDirection,
        target_number: str = "",
        target_name: str = "",
        purpose: str = "",
        language: str = "",
        instructions: str = "",
    ) -> CallState:
        from pincer.voice.language import resolve_call_language

        state = CallState(
            call_sid=call_sid,
            direction=direction,
            caller_number=caller,
            target_number=target_number,
            target_name=target_name,
            purpose=purpose,
            instructions=instructions,
            language=resolve_call_language(self._settings, language),
            engine_type=self.engine_name,
        )
        self._active_calls[call_sid] = state
        # This path registers a call that is already live (inbound, or an
        # outbound state recovered at setup), so it counts as answered and
        # fires the callback below; mark_call_answered() then no-ops.
        state.metadata["answered_at"] = datetime.now(UTC)
        # Conversation analytics start counting the moment the call exists.
        # `analytics_method` is a per-engine class attribute, so the record
        # carries its own provenance rather than being labelled later by
        # whoever happens to read it.
        from pincer.voice.analytics import ensure_accumulator

        ensure_accumulator(state, self.analytics_method)
        logger.info("Call registered: %s (%s) from %s, language=%s", call_sid, direction, caller, state.language)
        if self._on_call_start_callback is not None:
            try:
                await self._on_call_start_callback(call_sid, state)
            except Exception:
                logger.exception("on_call_start callback failed [%s]", call_sid)
        return state

    async def _unregister_call(self, call_sid: str) -> CallState | None:
        state = self._active_calls.pop(call_sid, None)
        if state:
            state.ended_at = datetime.now(UTC)
            self._remember_ended(call_sid)
            logger.info(
                "Call ended: %s duration=%ds",
                call_sid,
                state.duration_seconds,
            )
        return state

    def _remember_ended(self, call_sid: str) -> None:
        """Record that this call HAD state and has been torn down.

        Teardown schedules the post-call pipeline as a background task, so for
        a moment afterwards `get_call_state()` returns None for a call that is
        very much accounted for. A status webhook arriving in that window must
        not read the miss as "this call never connected".
        """
        self._ended_calls[call_sid] = time.monotonic()
        cutoff = time.monotonic() - _ENDED_CALL_TTL_S
        for sid in [s for s, at in self._ended_calls.items() if at < cutoff]:
            self._ended_calls.pop(sid, None)
        while len(self._ended_calls) > _MAX_ENDED_CALLS:
            self._ended_calls.pop(next(iter(self._ended_calls)), None)

    def was_recently_ended(self, call_sid: str) -> bool:
        """Whether `end_call` already tore this call down.

        `end_call` always owns the final user message from that point on —
        either through the post-call pipeline it schedules or through the
        fallback status it sends directly — so a caller that finds no live
        state should stay quiet rather than send its own generic ending.
        """
        started = self._ended_calls.get(call_sid)
        return started is not None and (time.monotonic() - started) < _ENDED_CALL_TTL_S

    @property
    @abstractmethod
    def engine_name(self) -> str: ...

    # Talk-time provenance for this engine (analytics.METHOD_EXACT/ESTIMATED).
    # Media Streams sees real μ-law byte counts and STT word timings;
    # ConversationRelay hands audio to Twilio and never reports playout, so
    # its numbers can only ever be estimates from character counts.
    analytics_method: str = "estimated"


def build_dial_twiml(
    target_number: str,
    *,
    timeout_s: int = 30,
    action_url: str = "",
    announce: str = "",
    language: str = "",
) -> str:
    """<Response>[<Say>announce</Say>]<Dial timeout=".." action="..">+49…</Dial></Response>.

    The announcement rides in the same TwiML (Twilio's <Say>) so it cannot be
    cut off by the redirect — the relay socket is gone the moment the call is
    updated."""
    from xml.sax.saxutils import escape

    from pincer.voice.language import relay_language

    say = f'<Say language="{relay_language(language or "en")}">{escape(announce)}</Say>' if announce else ""
    action = f' action="{escape(action_url, {chr(34): "&quot;"})}" method="POST"' if action_url else ""
    return f'<Response>{say}<Dial timeout="{int(timeout_s)}"{action}>{escape(target_number)}</Dial></Response>'


class ConversationRelayEngine(VoiceEngine):
    """Phase 1: Twilio ConversationRelay — text in/out, Twilio handles audio.

    Twilio performs STT and TTS; we only exchange text via a webhook.
    Fastest to ship, higher latency (~2-3s).
    """

    @property
    def engine_name(self) -> str:
        return "conversation_relay"

    analytics_method = "estimated"

    async def on_call_start(
        self,
        call_sid: str,
        caller: str,
        direction: CallDirection,
        target_number: str = "",
        target_name: str = "",
        purpose: str = "",
        language: str = "",
        instructions: str = "",
    ) -> CallState:
        state = await self._register_call(
            call_sid,
            caller,
            direction,
            target_number,
            target_name,
            purpose,
            language,
            instructions,
        )
        logger.info("ConversationRelay call started: %s", call_sid)
        return state

    async def on_speech_input(self, call_sid: str, text_or_audio: Any) -> None:
        """Process text input from ConversationRelay webhook."""
        text = str(text_or_audio)
        logger.info("CR speech input [%s]: %s", call_sid, text[:80])
        state = self._active_calls.get(call_sid)
        if state and text.strip():
            state.mark_caller_spoke()
            from pincer.voice.analytics import get_accumulator

            accumulator = get_accumulator(state)
            if accumulator is not None:
                accumulator.caller_text(text, state.language)
        if self._on_speech_callback:
            await self._on_speech_callback(call_sid, text)

    async def send_speech(self, call_sid: str, text_or_audio: Any, *, last: bool = True) -> bool:
        """Send text response — Twilio converts to speech.

        Returns True only when the token was handed to the ConversationRelay
        WebSocket; a False return means the caller heard nothing.
        ``last=False`` streams a partial sentence of the current reply
        (Sprint 5): CR synthesizes it immediately while the LLM keeps writing.
        """
        text = str(text_or_audio)
        state = self._active_calls.get(call_sid)
        if not state:
            logger.warning("send_speech for unknown call: %s", call_sid)
            return False

        delivered = False
        ws = state.metadata.get("websocket")
        if ws:
            from pincer.voice.language import relay_language

            # Per-token language attribute pins TTS to the call language
            # (state.language is the single source of truth; it only changes
            # via language_guard.perform_switch on explicit caller request).
            msg = json.dumps({"type": "text", "token": text, "last": last, "lang": relay_language(state.language)})
            try:
                from pincer.voice.telemetry.tracer import current_turn_tracer

                turn = current_turn_tracer()
                if turn is not None and text:
                    turn.stamp("audio.queued")
                await ws.send_text(msg)
                delivered = True
                if turn is not None and text:
                    # ConversationRelay synthesises inside Twilio, so the first
                    # TEXT TOKEN handed over is the earliest observable point of
                    # the response. It is not audio and is never labelled as such.
                    turn.first_audio(kind="text_token", engine="conversation_relay")
                from pincer.voice.analytics import get_accumulator

                accumulator = get_accumulator(state)
                if accumulator is not None:
                    accumulator.agent_text(text, state.language)
            except Exception:
                logger.exception("CR websocket send failed [%s] — agent output DROPPED", call_sid)
        else:
            logger.warning("send_speech with no CR websocket [%s] — agent output DROPPED", call_sid)
        logger.info("CR speech output [%s] delivered=%s: %s", call_sid, delivered, text[:80])
        return delivered

    async def interrupt_speech(self, call_sid: str) -> None:
        # ConversationRelay handles barge-in natively; its "interrupt" message
        # is informational. Sending Media-Streams-style {"type": "clear"} here
        # is an invalid CR message (Twilio error 64107) — nothing to send.
        # The count is exact on both engines even though the timing is not.
        from pincer.voice.analytics import get_accumulator
        from pincer.voice.telemetry import hooks as telemetry

        accumulator = get_accumulator(self._active_calls.get(call_sid))
        if accumulator is not None:
            accumulator.interruption()
        # Reported, not performed: Twilio already stopped the audio. There is no
        # cancellation of ours to time, which is why interruption latency is
        # `Unavailable` on this engine.
        telemetry.interruption_reported(call_sid, engine="conversation_relay")
        logger.debug("CR interrupt [%s]", call_sid)

    async def transfer_call(
        self,
        call_sid: str,
        target_number: str,
        *,
        timeout_s: int = 30,
        action_url: str = "",
        announce: str = "",
        language: str = "",
    ) -> None:
        """Redirect the live call into <Dial>. With ``action_url`` (Sprint 12
        receptionist) Twilio reports the dial result there, so a busy / no-answer
        target can fall back to message-taking instead of dropping the caller."""
        # the media socket closes on the <Dial> redirect; that is not a hangup
        live = self._active_calls.get(call_sid)
        if live is not None:
            live.metadata["transferring"] = True
        try:
            from twilio.rest import Client

            client = Client(
                self._settings.twilio_account_sid,
                self._settings.twilio_auth_token.get_secret_value(),
            )
            call = client.calls(call_sid)
            twiml = build_dial_twiml(
                target_number, timeout_s=timeout_s, action_url=action_url, announce=announce, language=language
            )
            call.update(twiml=twiml)
            logger.info("Call %s transferred to %s", call_sid, target_number)
        except Exception:
            logger.exception("Transfer failed for call %s", call_sid)
            raise

    async def end_call(self, call_sid: str) -> None:
        try:
            from twilio.rest import Client

            client = Client(
                self._settings.twilio_account_sid,
                self._settings.twilio_auth_token.get_secret_value(),
            )
            client.calls(call_sid).update(status="completed")
        except Exception:
            logger.exception("Failed to end call %s", call_sid)
        finally:
            state = await self._unregister_call(call_sid)
            if state and self._on_call_end_callback:
                await self._on_call_end_callback(call_sid, state)

    async def send_dtmf(self, call_sid: str, digits: str) -> None:
        try:
            from twilio.rest import Client

            client = Client(
                self._settings.twilio_account_sid,
                self._settings.twilio_auth_token.get_secret_value(),
            )
            twiml = f'<Response><Play digits="{digits}"/></Response>'
            client.calls(call_sid).update(twiml=twiml)
            logger.info("DTMF sent [%s]: %s", call_sid, digits)
        except Exception:
            logger.exception("DTMF failed for call %s", call_sid)
            raise

    async def close_media_stream(self, call_sid: str) -> None:
        """No-op for ConversationRelay; MediaStreamEngine overrides."""
        pass


class MediaStreamEngine(VoiceEngine):
    """Phase 2: Twilio Media Streams — raw mu-law audio via WebSocket.

    We run our own STT (Deepgram) and TTS (ElevenLabs) pipeline
    for lower latency (~0.8-1.5s) and custom voices.
    """

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._stt_provider = None
        self._tts_provider = None
        self._barge_in_controller = None

    @property
    def engine_name(self) -> str:
        return "media_streams"

    analytics_method = "exact"

    async def _ensure_providers(self) -> None:
        if self._stt_provider is None:
            from pincer.voice.stt import DeepgramSTT

            api_key = self._settings.deepgram_api_key.get_secret_value()
            if api_key:
                self._stt_provider = DeepgramSTT(api_key=api_key)

        if self._tts_provider is None:
            from pincer.voice.language import elevenlabs_model_for
            from pincer.voice.tts import OUTPUT_ULAW_8000, ElevenLabsTTS

            api_key = self._settings.elevenlabs_api_key.get_secret_value()
            if api_key:
                self._tts_provider = ElevenLabsTTS(
                    api_key=api_key,
                    voice_id=self._settings.elevenlabs_voice_id or None,
                    model=elevenlabs_model_for(self._settings),
                    stability=_safe_float(getattr(self._settings, "elevenlabs_stability", 0.5), 0.5),
                    similarity=_safe_float(getattr(self._settings, "elevenlabs_similarity", 0.75), 0.75),
                    speed=_safe_float(getattr(self._settings, "elevenlabs_speed", 1.0), 1.0),
                    style=_safe_float(getattr(self._settings, "elevenlabs_style", 0.0), 0.0),
                    output_format=str(getattr(self._settings, "elevenlabs_output_format", "") or OUTPUT_ULAW_8000),
                )

    async def on_call_start(
        self,
        call_sid: str,
        caller: str,
        direction: CallDirection,
        target_number: str = "",
        target_name: str = "",
        purpose: str = "",
        language: str = "",
        instructions: str = "",
    ) -> CallState:
        await self._ensure_providers()
        state = await self._register_call(
            call_sid,
            caller,
            direction,
            target_number,
            target_name,
            purpose,
            language,
            instructions,
        )
        logger.info("MediaStream call started: %s", call_sid)
        return state

    # Consecutive low-confidence transcripts tolerated before passing input
    # through anyway (avoids an ask-to-repeat loop with a noisy line).
    MAX_LOW_CONFIDENCE_RETRIES = 2

    async def setup_media_stream_stt(self, call_sid: str, stream_sid: str) -> None:
        """Create STT stream and transcript consumer when Media Streams WebSocket starts."""
        state = self._active_calls.get(call_sid)
        if not state or not self._stt_provider:
            return

        state.metadata["stream_sid"] = stream_sid

        from pincer.voice.stt import stt_config_for_language

        config = stt_config_for_language(state.language, self._settings)
        from pincer.voice.telemetry.clock import mono_ns as _mono_ns

        stt_started_ns = _mono_ns()
        stt_stream = await self._stt_provider.start_stream(config)
        state.metadata["stt_stream"] = stt_stream
        # Deepgram word times are offsets into the AUDIO timeline of this
        # stream. Anchoring them to the monotonic instant the stream opened is
        # what turns "the caller stopped talking at 4.12 s" into a stamp on the
        # same clock every other stage is measured on.
        state.metadata["stt_started_ns"] = stt_started_ns
        state.metadata["stt_endpointing_ms"] = float(config.endpointing)
        state.metadata["stt_utterance_end_ms"] = float(config.utterance_end_ms)

        async def _handle_final_transcript(transcript: Any) -> None:
            text = transcript.text.strip()
            if not text or not self._on_speech_callback:
                return
            state.mark_caller_spoke()
            # Deepgram word timings are real measurements of when the caller
            # spoke, which is what makes this engine's talk time `exact`.
            # Recorded before the confidence gate: a mumbled utterance the
            # agent asks to repeat was still time the caller spent talking.
            from pincer.voice.analytics import get_accumulator

            accumulator = get_accumulator(state)
            words = list(getattr(transcript, "words", None) or [])
            if accumulator is not None and words:
                accumulator.caller_span(words[0].start, words[-1].end)
            if words:
                # The one engine where "when did the caller stop speaking" is an
                # observation rather than a guess.
                state.metadata["speech_end_ns"] = int(
                    state.metadata.get("stt_started_ns", 0) + words[-1].end * 1_000_000_000
                )
                state.metadata["speech_start_ns"] = int(
                    state.metadata.get("stt_started_ns", 0) + words[0].start * 1_000_000_000
                )
            else:
                state.metadata.pop("speech_end_ns", None)
            # Misheard-input policy (Sprint 1): on low STT confidence ask to
            # repeat instead of acting on a guess. Confidence 0.0 means the
            # provider sent none — treat as trustworthy rather than looping.
            threshold = float(getattr(self._settings, "voice_stt_min_confidence", 0.0) or 0.0)
            confidence = float(getattr(transcript, "confidence", 0.0) or 0.0)
            low_count = int(state.metadata.get("low_confidence_count", 0))
            if 0.0 < confidence < threshold and low_count < self.MAX_LOW_CONFIDENCE_RETRIES:
                state.metadata["low_confidence_count"] = low_count + 1
                logger.info(
                    "Low STT confidence [%s]: %.2f < %.2f — asking to repeat",
                    call_sid,
                    confidence,
                    threshold,
                )
                from pincer.voice.language import de_formality
                from pincer.voice.prompts import get_prompt

                await self.send_speech(
                    call_sid,
                    get_prompt("LOW_CONFIDENCE_REPLY", state.language, de_formality(self._settings)),
                )
                return
            state.metadata["low_confidence_count"] = 0
            await self._on_speech_callback(call_sid, text)
            # Per-utterance markers are consumed by the turn that just started;
            # leaving them would date the NEXT turn from this utterance.
            for marker in ("speech_end_ns", "speech_start_ns", "first_partial_ns"):
                state.metadata.pop(marker, None)

        async def _consume_transcripts() -> None:
            try:
                async for transcript in stt_stream.receive_transcripts():
                    if transcript.is_final:
                        await _handle_final_transcript(transcript)
                    elif transcript.text and "first_partial_ns" not in state.metadata:
                        # Interim results are not acted on, but the FIRST one
                        # dates recognition independently of the endpointing
                        # wait that follows it.
                        state.metadata["first_partial_ns"] = _mono_ns()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("STT transcript consumer error [%s]", call_sid)
                await self._recover_stt_stream(call_sid, stream_sid)
            finally:
                await stt_stream.close()
                if state.metadata.get("stt_stream") is stt_stream:
                    state.metadata.pop("stt_stream", None)

        task = asyncio.create_task(_consume_transcripts())
        state.metadata["stt_consumer_task"] = task
        logger.info("Media Stream STT started [%s]", call_sid)

    async def _recover_stt_stream(self, call_sid: str, stream_sid: str) -> None:
        """STT died mid-call: reconnect once, else end the call gracefully (T1.3)."""
        state = self._active_calls.get(call_sid)
        if not state:
            return
        from pincer.voice.telemetry import hooks as telemetry

        if not state.metadata.get("stt_reconnected"):
            state.metadata["stt_reconnected"] = True
            telemetry.reconnect(call_sid, "stt", attempt=1)
            logger.warning("STT stream died [%s] — attempting one reconnect", call_sid)
            try:
                await self.setup_media_stream_stt(call_sid, stream_sid)
                return
            except Exception:
                telemetry.error(call_sid, "stt_error", stage="stt", detail="reconnect_failed")
                logger.exception("STT reconnect failed [%s]", call_sid)
        # Second failure: speak an honest goodbye and end the call.
        from pincer.voice.language import de_formality
        from pincer.voice.prompts import get_prompt

        with contextlib.suppress(Exception):
            await self.send_speech(
                call_sid,
                get_prompt("STT_FAILURE_GOODBYE", state.language, de_formality(self._settings)),
            )
        with contextlib.suppress(Exception):
            await self.end_call(call_sid)

    async def on_speech_input(self, call_sid: str, text_or_audio: Any) -> None:
        """Process raw audio from Media Streams WebSocket.

        Twilio sends base64-encoded mu-law 8kHz. We decode, convert to PCM 16kHz,
        and send to Deepgram STT.
        """
        state = self._active_calls.get(call_sid)
        if not state:
            return

        # Pre-transcribed text (e.g. from fallback path) — pass directly to callback
        if isinstance(text_or_audio, str) and not _looks_like_base64(text_or_audio):
            if self._on_speech_callback:
                await self._on_speech_callback(call_sid, text_or_audio)
            return

        # Decode base64 payload from Twilio media event
        raw = base64.b64decode(text_or_audio) if isinstance(text_or_audio, str) else text_or_audio
        if not raw:
            return

        from pincer.voice.audio import mulaw8k_to_pcm16k

        pcm_16k = mulaw8k_to_pcm16k(raw)

        stt_stream = state.metadata.get("stt_stream")
        if stt_stream:
            await stt_stream.send_audio(pcm_16k)

    async def _clear_twilio_buffer(self, state: CallState) -> None:
        """Drop whatever Twilio still has buffered for playout.

        Note this flushes the WHOLE outbound buffer, not just the tail — so it
        is only safe when nothing the caller still needs to hear is in it.
        """
        ws = state.metadata.get("websocket")
        if not ws:
            return
        msg = json.dumps({"event": "clear", "streamSid": state.metadata.get("stream_sid", "")})
        await ws.send_text(msg)

    async def send_speech(self, call_sid: str, text_or_audio: Any, *, last: bool = True) -> bool:
        """Synthesize text to speech and send audio to Twilio.

        T4.5: a failed synthesis is retried once; a second failure takes the
        fallback path (spoken apology via Twilio <Say>, graceful hangup) —
        never dead air. Returns True only when audio was streamed.
        ``last`` is accepted for interface parity (Sprint 5 streaming) — each
        utterance is synthesized independently here, so it is a no-op.

        The retry RESUMES: delivery is tracked per sentence-segment, so a
        synthesis that dies mid-utterance restarts at the first segment the
        caller has not been sent, never from the top. Re-speaking the whole
        utterance made the caller hear the opening sentences twice while the
        transcript recorded them once.
        """
        state = self._active_calls.get(call_sid)
        if not state:
            logger.warning("send_speech for unknown call: %s", call_sid)
            return False

        text = str(text_or_audio)

        if not self._tts_provider:
            logger.warning("send_speech with no TTS provider [%s] — agent output DROPPED", call_sid)
            return False

        from pincer.voice.sentence_stream import split_into_segments

        segments = split_into_segments(text)
        if not segments:
            return False
        progress = _SpeechProgress()

        try:
            delivered = await self._stream_tts(call_sid, state, segments, progress)
        except Exception:
            from pincer.voice.telemetry import hooks as telemetry

            progress.attempts += 1
            telemetry.error(call_sid, "tts_error", stage="tts", attempt=progress.attempts)
            if progress.segments_done == 0 and progress.wrote_audio:
                # Only a fragment of the first segment is buffered, so dropping
                # the buffer costs the caller nothing and spares them hearing
                # that fragment before the retry repeats it. Once a segment has
                # completed we must NOT clear: `clear` flushes everything still
                # queued, which would silently swallow sentences the caller has
                # not heard yet. Losing content is worse than one stutter.
                with contextlib.suppress(Exception):
                    await self._clear_twilio_buffer(state)
            logger.warning(
                "TTS synthesis failed [%s] after %d/%d segment(s) — resuming utterance",
                call_sid,
                progress.segments_done,
                len(segments),
            )
            try:
                delivered = await self._stream_tts(call_sid, state, segments, progress)
            except Exception:
                logger.exception("TTS retry failed [%s] — spoken fallback + hangup", call_sid)
                await self.fallback_and_end(call_sid)
                return False
        if not delivered:
            return False

        logger.info("MS speech output [%s] delivered=True: %s", call_sid, text[:80])
        return True

    async def _stream_tts(
        self,
        call_sid: str,
        state: CallState,
        segments: list[str],
        progress: _SpeechProgress,
    ) -> bool:
        """Stream an utterance's segments to the Twilio WebSocket.

        Picks up at ``progress.segments_done``, so a retry after a mid-stream
        failure delivers only what the caller has not been sent. A segment is
        marked done only once its whole stream has been written, so the segment
        that failed is re-synthesized from its own start — the finest boundary
        neural TTS can restart on without cutting mid-phoneme.

        Returns True when any audio has been written for this utterance — no
        socket means no synthesis (and no ElevenLabs spend). With ulaw_8000
        output the ElevenLabs bytes go to Twilio as-is; the Python resample
        only runs on the legacy pcm_16000 fallback path.
        """
        from pincer.voice.language import elevenlabs_model_for, voice_for
        from pincer.voice.tts import OUTPUT_PCM_16000

        ws = state.metadata.get("websocket")
        if not ws:
            logger.warning("send_speech with no MS websocket [%s] — agent output DROPPED", call_sid)
            return False
        stream_sid = state.metadata.get("stream_sid", "")
        voice_id = voice_for(self._settings, state.language)
        tts_model = elevenlabs_model_for(self._settings, state.language)
        needs_resample = getattr(self._tts_provider, "output_format", OUTPUT_PCM_16000) == OUTPUT_PCM_16000

        from pincer.voice.analytics import get_accumulator

        accumulator = get_accumulator(state)
        metrics = self.metrics_registry.get(call_sid) if self.metrics_registry else None

        from pincer.voice.telemetry.schema import EventName, SpanName, SpanStatus
        from pincer.voice.telemetry.tracer import current_turn_tracer

        turn = current_turn_tracer()

        started = time.monotonic()
        for index in range(progress.segments_done, len(segments)):
            segment = segments[index]
            # One span per segment AND per attempt: a resumed retry is a second
            # synthesis of that segment and reads as one on the waterfall.
            tts_span = (
                turn.open_span(
                    SpanName.TTS,
                    attempt=progress.attempts + 1,
                    segment=index,
                    characters=len(segment),
                    voice=voice_id,
                    model=tts_model,
                )
                if turn is not None
                else None
            )
            if turn is not None and index == progress.segments_done:
                turn.stamp(EventName.TTS_REQUEST, provider="elevenlabs", model=tts_model)
            async for audio_chunk in self._tts_provider.synthesize_stream(segment, voice=voice_id, model=tts_model):
                if progress.first_chunk_ms is None:
                    # Measured once for the utterance: playout begins when
                    # Twilio receives the first chunk, whichever attempt sent it.
                    progress.first_chunk_ms = (time.monotonic() - started) * 1000.0
                    if accumulator is not None:
                        accumulator.agent_audio_begin()
                    if metrics:
                        metrics.record_tts_first_chunk(progress.first_chunk_ms)
                    if turn is not None:
                        turn.stamp(EventName.TTS_FIRST_AUDIO, first_chunk_ms=round(progress.first_chunk_ms, 1))
                        turn.stamp(EventName.AUDIO_QUEUED, kind="audio")
                mulaw_data = audio_chunk
                if needs_resample:
                    from pincer.voice.audio import pcm16k_to_mulaw8k

                    mulaw_data = pcm16k_to_mulaw8k(audio_chunk)
                if mulaw_data:
                    payload = base64.b64encode(mulaw_data).decode("ascii")
                    msg = json.dumps(
                        {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": payload},
                        }
                    )
                    await ws.send_text(msg)
                    if turn is not None and not progress.wrote_audio:
                        # Written to the provider socket. SENT, not heard: Twilio
                        # buffers outbound audio and acknowledges nothing.
                        turn.first_audio(kind="audio", engine="media_streams")
                    progress.wrote_audio = True
                if accumulator is not None and mulaw_data:
                    # μ-law at 8 kHz: one byte is 0.125 ms of audio. This is the
                    # measurement the `exact` method is named for.
                    accumulator.agent_audio_bytes(len(mulaw_data))

            # Charged per segment actually synthesized, so a resumed retry does
            # not bill the segments it skipped (the old whole-utterance retry
            # counted every character twice).
            state.metadata["tts_characters"] = int(state.metadata.get("tts_characters", 0)) + len(segment)
            if metrics:
                metrics.record_tts_characters(len(segment))
            progress.segments_done = index + 1
            if tts_span is not None:
                tts_span.close(SpanStatus.OK)

        if turn is not None:
            turn.stamp(EventName.TTS_DONE, segments=len(segments))
        return progress.wrote_audio

    async def interrupt_speech(self, call_sid: str) -> None:
        state = self._active_calls.get(call_sid)
        if not state:
            return

        # Twilio's `clear` drops whatever is still buffered, so only the audio
        # that had time to play counts as agent speech.
        from pincer.voice.analytics import get_accumulator
        from pincer.voice.telemetry import hooks as telemetry

        accumulator = get_accumulator(state)
        if accumulator is not None:
            accumulator.agent_audio_cancelled()
            accumulator.interruption()

        detected_ns = telemetry.interruption_detected(call_sid, engine="media_streams")
        if self._tts_provider:
            await self._tts_provider.cancel()

        await self._clear_twilio_buffer(state)
        # Twilio does not acknowledge a `clear`, so this is when WE stopped
        # sending — audio already in its playout buffer may still be heard.
        telemetry.buffer_cleared(call_sid, since_ns=detected_ns, acknowledged=False)

        logger.debug("MS interrupt [%s]", call_sid)

    async def transfer_call(
        self,
        call_sid: str,
        target_number: str,
        *,
        timeout_s: int = 30,
        action_url: str = "",
        announce: str = "",
        language: str = "",
    ) -> None:
        # the media socket closes on the <Dial> redirect; that is not a hangup
        live = self._active_calls.get(call_sid)
        if live is not None:
            live.metadata["transferring"] = True
        try:
            from twilio.rest import Client

            client = Client(
                self._settings.twilio_account_sid,
                self._settings.twilio_auth_token.get_secret_value(),
            )
            twiml = build_dial_twiml(
                target_number, timeout_s=timeout_s, action_url=action_url, announce=announce, language=language
            )
            client.calls(call_sid).update(twiml=twiml)
            logger.info("Call %s transferred to %s", call_sid, target_number)
        except Exception:
            logger.exception("Transfer failed for call %s", call_sid)
            raise

    async def end_call(self, call_sid: str) -> None:
        await self.close_media_stream(call_sid)
        try:
            from twilio.rest import Client

            client = Client(
                self._settings.twilio_account_sid,
                self._settings.twilio_auth_token.get_secret_value(),
            )
            client.calls(call_sid).update(status="completed")
        except Exception:
            logger.exception("Failed to end call %s", call_sid)
        finally:
            state = await self._unregister_call(call_sid)
            if state and self._on_call_end_callback:
                await self._on_call_end_callback(call_sid, state)

    async def send_dtmf(self, call_sid: str, digits: str) -> None:
        try:
            from twilio.rest import Client

            client = Client(
                self._settings.twilio_account_sid,
                self._settings.twilio_auth_token.get_secret_value(),
            )
            twiml = f'<Response><Play digits="{digits}"/></Response>'
            client.calls(call_sid).update(twiml=twiml)
            logger.info("DTMF sent [%s]: %s", call_sid, digits)
        except Exception:
            logger.exception("DTMF failed for call %s", call_sid)
            raise

    async def close_media_stream(self, call_sid: str) -> None:
        """Close STT stream and cancel transcript consumer."""
        state = self._active_calls.get(call_sid)
        if not state:
            return
        stt_stream = state.metadata.pop("stt_stream", None)
        task = state.metadata.pop("stt_consumer_task", None)
        # The consumer task itself may drive call teardown (STT failure path);
        # never cancel-and-await the task we're currently running inside.
        if task and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if stt_stream:
            await stt_stream.close()
        logger.debug("Media stream STT closed [%s]", call_sid)


def get_voice_engine(settings: Settings) -> VoiceEngine:
    """Factory: return the configured voice engine implementation."""
    engine_type = settings.voice_engine.lower().strip()
    if engine_type == "media_streams":
        return MediaStreamEngine(settings)
    return ConversationRelayEngine(settings)
