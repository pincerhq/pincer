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


@dataclass
class CallState:
    """Runtime state for an active voice call."""

    call_sid: str
    direction: CallDirection
    caller_number: str
    target_number: str = ""
    target_name: str = ""
    purpose: str = ""
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
        # Optional VoiceMetricsRegistry, injected by VoiceChannel.set_engine
        self.metrics_registry: Any = None

    def set_on_speech(self, callback: Callable) -> None:
        self._on_speech_callback = callback

    def set_on_call_end(self, callback: Callable) -> None:
        self._on_call_end_callback = callback

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
    ) -> CallState: ...

    @abstractmethod
    async def on_speech_input(self, call_sid: str, text_or_audio: Any) -> None: ...

    @abstractmethod
    async def send_speech(self, call_sid: str, text_or_audio: Any) -> bool:
        """Speak on the call. Returns True only when the audio was actually
        handed to the transport — a False return means the caller heard
        nothing (transcripts must record that honestly)."""
        ...

    @abstractmethod
    async def interrupt_speech(self, call_sid: str) -> None: ...

    @abstractmethod
    async def transfer_call(self, call_sid: str, target_number: str) -> None: ...

    @abstractmethod
    async def end_call(self, call_sid: str) -> None: ...

    @abstractmethod
    async def send_dtmf(self, call_sid: str, digits: str) -> None: ...

    def get_call_state(self, call_sid: str) -> CallState | None:
        return self._active_calls.get(call_sid)

    @abstractmethod
    async def close_media_stream(self, call_sid: str) -> None:
        """Override in MediaStreamEngine to close STT stream and consumer."""

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

    async def _register_call(
        self,
        call_sid: str,
        caller: str,
        direction: CallDirection,
        target_number: str = "",
        target_name: str = "",
        purpose: str = "",
        language: str = "",
    ) -> CallState:
        from pincer.voice.language import resolve_call_language

        state = CallState(
            call_sid=call_sid,
            direction=direction,
            caller_number=caller,
            target_number=target_number,
            target_name=target_name,
            purpose=purpose,
            language=resolve_call_language(self._settings, language),
            engine_type=self.engine_name,
        )
        self._active_calls[call_sid] = state
        logger.info("Call registered: %s (%s) from %s, language=%s", call_sid, direction, caller, state.language)
        return state

    async def _unregister_call(self, call_sid: str) -> CallState | None:
        state = self._active_calls.pop(call_sid, None)
        if state:
            state.ended_at = datetime.now(UTC)
            logger.info(
                "Call ended: %s duration=%ds",
                call_sid,
                state.duration_seconds,
            )
        return state

    @property
    @abstractmethod
    def engine_name(self) -> str: ...


class ConversationRelayEngine(VoiceEngine):
    """Phase 1: Twilio ConversationRelay — text in/out, Twilio handles audio.

    Twilio performs STT and TTS; we only exchange text via a webhook.
    Fastest to ship, higher latency (~2-3s).
    """

    @property
    def engine_name(self) -> str:
        return "conversation_relay"

    async def on_call_start(
        self,
        call_sid: str,
        caller: str,
        direction: CallDirection,
        target_number: str = "",
        target_name: str = "",
        purpose: str = "",
        language: str = "",
    ) -> CallState:
        state = await self._register_call(
            call_sid,
            caller,
            direction,
            target_number,
            target_name,
            purpose,
            language,
        )
        logger.info("ConversationRelay call started: %s", call_sid)
        return state

    async def on_speech_input(self, call_sid: str, text_or_audio: Any) -> None:
        """Process text input from ConversationRelay webhook."""
        text = str(text_or_audio)
        logger.info("CR speech input [%s]: %s", call_sid, text[:80])
        state = self._active_calls.get(call_sid)
        if state and text.strip():
            # A conversing counterpart — lets the status webhook ignore
            # late/false AMD "machine" verdicts instead of killing the call.
            state.metadata["caller_spoke"] = True
        if self._on_speech_callback:
            await self._on_speech_callback(call_sid, text)

    async def send_speech(self, call_sid: str, text_or_audio: Any) -> bool:
        """Send text response — Twilio converts to speech.

        Returns True only when the token was handed to the ConversationRelay
        WebSocket; a False return means the caller heard nothing.
        """
        text = str(text_or_audio)
        state = self._active_calls.get(call_sid)
        if not state:
            logger.warning("send_speech for unknown call: %s", call_sid)
            return False

        delivered = False
        ws = state.metadata.get("websocket")
        if ws:
            msg = json.dumps({"type": "text", "token": text, "last": True})
            try:
                await ws.send_text(msg)
                delivered = True
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
        logger.debug("CR interrupt [%s]", call_sid)

    async def transfer_call(self, call_sid: str, target_number: str) -> None:
        try:
            from twilio.rest import Client

            client = Client(
                self._settings.twilio_account_sid,
                self._settings.twilio_auth_token.get_secret_value(),
            )
            call = client.calls(call_sid)
            twiml = f"<Response><Dial>{target_number}</Dial></Response>"
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

        config = stt_config_for_language(state.language)
        stt_stream = await self._stt_provider.start_stream(config)
        state.metadata["stt_stream"] = stt_stream

        async def _handle_final_transcript(transcript: Any) -> None:
            text = transcript.text.strip()
            if not text or not self._on_speech_callback:
                return
            # See ConversationRelayEngine.on_speech_input: shields a live
            # conversation from late/false AMD "machine" verdicts.
            state.metadata["caller_spoke"] = True
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

        async def _consume_transcripts() -> None:
            try:
                async for transcript in stt_stream.receive_transcripts():
                    if transcript.is_final:
                        await _handle_final_transcript(transcript)
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
        if not state.metadata.get("stt_reconnected"):
            state.metadata["stt_reconnected"] = True
            logger.warning("STT stream died [%s] — attempting one reconnect", call_sid)
            try:
                await self.setup_media_stream_stt(call_sid, stream_sid)
                return
            except Exception:
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

    async def send_speech(self, call_sid: str, text_or_audio: Any) -> bool:
        """Synthesize text to speech and send audio to Twilio.

        T4.5: a failed synthesis is retried once; a second failure takes the
        fallback path (spoken apology via Twilio <Say>, graceful hangup) —
        never dead air. Returns True only when audio was streamed.
        """
        state = self._active_calls.get(call_sid)
        if not state:
            logger.warning("send_speech for unknown call: %s", call_sid)
            return False

        text = str(text_or_audio)

        if not self._tts_provider:
            logger.warning("send_speech with no TTS provider [%s] — agent output DROPPED", call_sid)
            return False

        try:
            await self._stream_tts(call_sid, state, text)
        except Exception:
            logger.warning("TTS synthesis failed [%s] — retrying utterance once", call_sid)
            try:
                await self._stream_tts(call_sid, state, text)
            except Exception:
                logger.exception("TTS retry failed [%s] — spoken fallback + hangup", call_sid)
                await self.fallback_and_end(call_sid)
                return False

        logger.info("MS speech output [%s] delivered=True: %s", call_sid, text[:80])
        return True

    async def _stream_tts(self, call_sid: str, state: CallState, text: str) -> None:
        """One synthesis attempt: stream audio chunks to the Twilio WebSocket.

        With ulaw_8000 output the ElevenLabs bytes go to Twilio as-is; the
        Python resample only runs on the legacy pcm_16000 fallback path.
        """
        import time

        from pincer.voice.language import elevenlabs_model_for, voice_for
        from pincer.voice.tts import OUTPUT_PCM_16000

        ws = state.metadata.get("websocket")
        stream_sid = state.metadata.get("stream_sid", "")
        voice_id = voice_for(self._settings, state.language)
        tts_model = elevenlabs_model_for(self._settings, state.language)
        needs_resample = getattr(self._tts_provider, "output_format", OUTPUT_PCM_16000) == OUTPUT_PCM_16000

        started = time.monotonic()
        first_chunk_ms: float | None = None
        async for audio_chunk in self._tts_provider.synthesize_stream(text, voice=voice_id, model=tts_model):
            if first_chunk_ms is None:
                first_chunk_ms = (time.monotonic() - started) * 1000.0
            mulaw_data = audio_chunk
            if needs_resample:
                from pincer.voice.audio import pcm16k_to_mulaw8k

                mulaw_data = pcm16k_to_mulaw8k(audio_chunk)
            if ws and mulaw_data:
                payload = base64.b64encode(mulaw_data).decode("ascii")
                msg = json.dumps(
                    {
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": payload},
                    }
                )
                await ws.send_text(msg)

        # Latency guard + per-call character count for cost tracking (T4.2/T4.5)
        state.metadata["tts_characters"] = int(state.metadata.get("tts_characters", 0)) + len(text)
        metrics = self.metrics_registry.get(call_sid) if self.metrics_registry else None
        if metrics:
            metrics.record_tts_characters(len(text))
            if first_chunk_ms is not None:
                metrics.record_tts_first_chunk(first_chunk_ms)

    async def interrupt_speech(self, call_sid: str) -> None:
        state = self._active_calls.get(call_sid)
        if not state:
            return

        if self._tts_provider:
            await self._tts_provider.cancel()

        ws = state.metadata.get("websocket")
        stream_sid = state.metadata.get("stream_sid", "")
        if ws:
            msg = json.dumps({"event": "clear", "streamSid": stream_sid})
            await ws.send_text(msg)

        logger.debug("MS interrupt [%s]", call_sid)

    async def transfer_call(self, call_sid: str, target_number: str) -> None:
        try:
            from twilio.rest import Client

            client = Client(
                self._settings.twilio_account_sid,
                self._settings.twilio_auth_token.get_secret_value(),
            )
            twiml = f"<Response><Dial>{target_number}</Dial></Response>"
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
