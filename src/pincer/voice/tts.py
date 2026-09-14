"""
Text-to-Speech provider abstraction + ElevenLabs streaming implementation.

Supports streaming text-to-speech synthesis with mid-stream cancellation
for barge-in handling. Sprint 4: configurable model/voice settings, native
ulaw_8000 telephony output (no Python resampling in the hot path), and a
time-to-first-audio latency guard.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # ElevenLabs Rachel voice
DEFAULT_MODEL = "eleven_flash_v2_5"  # multilingual + low latency
OUTPUT_ULAW_8000 = "ulaw_8000"
OUTPUT_PCM_16000 = "pcm_16000"

# Above this time-to-first-audio-chunk, log a WARNING (feeds Sprint 1 metrics)
FIRST_CHUNK_WARN_MS = 800.0


class TTSSynthesisError(Exception):
    """Raised when a synthesis stream fails (not cancelled) — lets the engine
    retry the utterance once and then fall back gracefully (T4.5)."""


class TTSProvider(ABC):
    """Abstract text-to-speech provider."""

    #: Audio format of the yielded chunks (ulaw_8000 or pcm_16000)
    output_format: str = OUTPUT_PCM_16000

    @abstractmethod
    def synthesize_stream(
        self,
        text: str,
        voice: str | None = None,
        model: str | None = None,
    ) -> AsyncIterator[bytes]:
        """Yield audio chunks (in `output_format`) as the text is synthesized.

        `voice`/`model` override the provider defaults per call (Sprint 2:
        per-language voices; Sprint 4: one multilingual model).
        """
        ...

    @abstractmethod
    async def cancel(self) -> None:
        """Cancel current synthesis (for barge-in)."""
        ...


class ElevenLabsTTS(TTSProvider):
    """ElevenLabs streaming TTS via WebSocket API."""

    def __init__(
        self,
        api_key: str,
        voice_id: str | None = None,
        model: str = DEFAULT_MODEL,
        base_url: str = "wss://api.elevenlabs.io",
        stability: float = 0.5,
        similarity: float = 0.75,
        speed: float = 1.0,
        style: float = 0.0,
        output_format: str = OUTPUT_ULAW_8000,
    ) -> None:
        self._api_key = api_key
        self._voice_id = voice_id or DEFAULT_VOICE_ID
        self._model = model
        self._base_url = base_url
        self._stability = stability
        self._similarity = similarity
        self._speed = speed
        self._style = style
        if output_format not in (OUTPUT_ULAW_8000, OUTPUT_PCM_16000):
            output_format = OUTPUT_ULAW_8000
        self.output_format = output_format
        self._ws: Any = None
        self._cancelled = False
        #: Time-to-first-audio of the most recent utterance (ms), for metrics
        self.last_first_chunk_ms: float | None = None

    def _voice_settings(self) -> dict[str, float]:
        settings: dict[str, float] = {
            "stability": self._stability,
            "similarity_boost": self._similarity,
        }
        if self._speed != 1.0:
            settings["speed"] = self._speed
        if self._style > 0.0:
            settings["style"] = self._style
        return settings

    async def synthesize_stream(
        self,
        text: str,
        voice: str | None = None,
        model: str | None = None,
    ) -> AsyncIterator[bytes]:
        """Stream synthesized audio chunks for the given text.

        Raises TTSSynthesisError on stream failure (unless cancelled), so the
        caller can retry once and then take the fallback path — never dead air.
        """
        self._cancelled = False
        voice_id = voice or self._voice_id
        model_id = model or self._model

        try:
            import websockets
        except ImportError as e:
            raise ImportError(
                "websockets is required for ElevenLabs TTS. Install with: uv pip install websockets"
            ) from e

        url = (
            f"{self._base_url}/v1/text-to-speech/{voice_id}/stream-input"
            f"?model_id={model_id}&output_format={self.output_format}"
        )

        started = time.monotonic()
        first_chunk_at: float | None = None

        try:
            self._ws = await websockets.connect(
                url,
                additional_headers={"xi-api-key": self._api_key},
            )

            init_msg = json.dumps(
                {
                    "text": " ",
                    "voice_settings": self._voice_settings(),
                    "xi_api_key": self._api_key,
                }
            )
            await self._ws.send(init_msg)

            sentences = _split_sentences(text)
            for sentence in sentences:
                if self._cancelled:
                    break
                msg = json.dumps({"text": sentence + " ", "try_trigger_generation": True})
                await self._ws.send(msg)

            close_msg = json.dumps({"text": ""})
            await self._ws.send(close_msg)

            async for raw in self._ws:
                if self._cancelled:
                    break

                chunk: bytes | None = None
                if isinstance(raw, bytes):
                    chunk = raw
                else:
                    data = raw if isinstance(raw, str) else raw.decode()
                    try:
                        parsed = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    audio_b64 = parsed.get("audio")
                    if audio_b64:
                        import base64

                        chunk = base64.b64decode(audio_b64)

                    if parsed.get("isFinal"):
                        if chunk:
                            yield chunk
                        break

                if chunk:
                    if first_chunk_at is None:
                        first_chunk_at = time.monotonic()
                        self.last_first_chunk_ms = (first_chunk_at - started) * 1000.0
                        if self.last_first_chunk_ms > FIRST_CHUNK_WARN_MS:
                            logger.warning(
                                "ElevenLabs time-to-first-audio %.0fms (>%.0fms) voice=%s model=%s",
                                self.last_first_chunk_ms,
                                FIRST_CHUNK_WARN_MS,
                                voice_id,
                                model_id,
                            )
                    yield chunk

        except Exception as e:
            if not self._cancelled:
                logger.exception("ElevenLabs TTS stream error")
                raise TTSSynthesisError(str(e)) from e
        finally:
            await self._close_ws()

    async def cancel(self) -> None:
        self._cancelled = True
        await self._close_ws()
        logger.debug("ElevenLabs TTS cancelled")

    async def _close_ws(self) -> None:
        ws = self._ws
        self._ws = None
        if ws:
            with contextlib.suppress(Exception):
                await ws.close()


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences for chunked TTS synthesis."""
    import re

    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    return [s for s in sentences if s.strip()]
