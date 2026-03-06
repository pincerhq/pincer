"""
Barge-in controller — detects when a user speaks while the agent is talking.

Uses Voice Activity Detection (VAD) to identify user speech during TTS
playback, then cancels the current TTS stream and switches to listening mode.
Target: <500ms from speech onset to TTS stop.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pincer.voice.engine import VoiceEngine

logger = logging.getLogger(__name__)

SPEECH_THRESHOLD_MS = 200
ENERGY_THRESHOLD = 500
SAMPLE_RATE = 8000


@dataclass
class BargeInEvent:
    """Emitted when barge-in is detected."""

    call_sid: str
    timestamp: float
    speech_energy: float
    tts_was_active: bool


class BargeInController:
    """Monitors incoming audio during TTS playback for barge-in detection.

    When sustained speech (>200ms) is detected during active TTS:
    1. Cancel current TTS synthesis
    2. Clear audio output buffer
    3. Notify the agent brain that response was interrupted
    """

    def __init__(
        self,
        engine: VoiceEngine,
        speech_threshold_ms: int = SPEECH_THRESHOLD_MS,
        energy_threshold: float = ENERGY_THRESHOLD,
    ) -> None:
        self._engine = engine
        self._speech_threshold_ms = speech_threshold_ms
        self._energy_threshold = energy_threshold
        self._tts_active: dict[str, bool] = {}
        self._speech_start: dict[str, float | None] = {}
        self._on_barge_in: Any = None
        self._vad_model: Any = None

    def set_on_barge_in(self, callback: Any) -> None:
        self._on_barge_in = callback

    def set_tts_active(self, call_sid: str, active: bool) -> None:
        self._tts_active[call_sid] = active
        if not active:
            self._speech_start.pop(call_sid, None)

    def _compute_energy(self, audio_bytes: bytes) -> float:
        """Compute RMS energy of PCM audio."""
        n_samples = len(audio_bytes) // 2
        if n_samples == 0:
            return 0.0
        samples = struct.unpack(f"<{n_samples}h", audio_bytes)
        rms = (sum(s * s for s in samples) / n_samples) ** 0.5
        return rms

    async def process_audio(self, call_sid: str, audio_bytes: bytes) -> BargeInEvent | None:
        """Process incoming audio chunk and detect barge-in.

        Returns a BargeInEvent if barge-in was detected, None otherwise.
        """
        if not self._tts_active.get(call_sid, False):
            return None

        energy = self._compute_energy(audio_bytes)
        is_speech = energy > self._energy_threshold

        if is_speech:
            if self._speech_start.get(call_sid) is None:
                self._speech_start[call_sid] = time.monotonic()

            speech_start = self._speech_start[call_sid]
            elapsed_ms = (time.monotonic() - speech_start) * 1000

            if elapsed_ms >= self._speech_threshold_ms:
                logger.info(
                    "Barge-in detected [%s]: energy=%.0f, duration=%.0fms",
                    call_sid, energy, elapsed_ms,
                )

                await self._engine.interrupt_speech(call_sid)
                self._tts_active[call_sid] = False
                self._speech_start.pop(call_sid, None)

                event = BargeInEvent(
                    call_sid=call_sid,
                    timestamp=time.monotonic(),
                    speech_energy=energy,
                    tts_was_active=True,
                )

                if self._on_barge_in:
                    await self._on_barge_in(event)

                return event
        else:
            self._speech_start.pop(call_sid, None)

        return None

    def cleanup_call(self, call_sid: str) -> None:
        """Remove tracking state for an ended call."""
        self._tts_active.pop(call_sid, None)
        self._speech_start.pop(call_sid, None)
