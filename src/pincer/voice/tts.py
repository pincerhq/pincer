"""
Text-to-Speech provider abstraction + ElevenLabs streaming implementation.

Supports streaming text-to-speech synthesis with mid-stream cancellation
for barge-in handling.
"""

from __future__ import annotations

import contextlib
import json
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # ElevenLabs Rachel voice
DEFAULT_MODEL = "eleven_turbo_v2"


class TTSProvider(ABC):
    """Abstract text-to-speech provider."""

    @abstractmethod
    async def synthesize_stream(
        self,
        text: str,
        voice: str | None = None,
    ) -> AsyncIterator[bytes]:
        """Yield PCM 16kHz audio chunks as the text is synthesized."""
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
    ) -> None:
        self._api_key = api_key
        self._voice_id = voice_id or DEFAULT_VOICE_ID
        self._model = model
        self._base_url = base_url
        self._ws: Any = None
        self._cancelled = False

    async def synthesize_stream(
        self,
        text: str,
        voice: str | None = None,
    ) -> AsyncIterator[bytes]:
        """Stream synthesized audio chunks for the given text."""
        self._cancelled = False
        voice_id = voice or self._voice_id

        try:
            import websockets
        except ImportError as e:
            raise ImportError(
                "websockets is required for ElevenLabs TTS. Install with: uv pip install websockets"
            ) from e

        url = (
            f"{self._base_url}/v1/text-to-speech/{voice_id}/stream-input?model_id={self._model}&output_format=pcm_16000"
        )

        try:
            self._ws = await websockets.connect(
                url,
                additional_headers={"xi-api-key": self._api_key},
            )

            init_msg = json.dumps(
                {
                    "text": " ",
                    "voice_settings": {
                        "stability": 0.5,
                        "similarity_boost": 0.75,
                    },
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

                if isinstance(raw, bytes):
                    yield raw
                    continue

                data = raw if isinstance(raw, str) else raw.decode()
                try:
                    parsed = json.loads(data)
                except json.JSONDecodeError:
                    continue

                audio_b64 = parsed.get("audio")
                if audio_b64:
                    import base64

                    yield base64.b64decode(audio_b64)

                if parsed.get("isFinal"):
                    break

        except Exception:
            if not self._cancelled:
                logger.exception("ElevenLabs TTS stream error")
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
