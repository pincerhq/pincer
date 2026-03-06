"""
Speech-to-Text provider abstraction + Deepgram streaming implementation.

Supports streaming audio input with partial/final transcript output.
Deepgram Nova-2 is the default for real-time telephony STT.
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)


@dataclass
class TranscriptWord:
    word: str
    start: float = 0.0
    end: float = 0.0
    confidence: float = 0.0


@dataclass
class Transcript:
    text: str
    is_final: bool = False
    confidence: float = 0.0
    words: list[TranscriptWord] = field(default_factory=list)
    language: str = ""
    speech_final: bool = False


@dataclass
class STTConfig:
    model: str = "nova-2"
    language: str = "en"
    sample_rate: int = 16000
    channels: int = 1
    encoding: str = "linear16"
    smart_format: bool = True
    interim_results: bool = True
    endpointing: int = 300
    utterance_end_ms: int = 1000
    vad_events: bool = True


class STTStream(ABC):
    """Active streaming transcription session."""

    @abstractmethod
    async def send_audio(self, audio_bytes: bytes) -> None:
        ...

    @abstractmethod
    async def receive_transcripts(self) -> AsyncIterator[Transcript]:
        ...

    @abstractmethod
    async def close(self) -> None:
        ...


class STTProvider(ABC):
    """Abstract speech-to-text provider."""

    @abstractmethod
    async def start_stream(self, config: STTConfig | None = None) -> STTStream:
        ...


class DeepgramSTTStream(STTStream):
    """Streaming STT session using Deepgram WebSocket API."""

    def __init__(self, ws: Any, config: STTConfig) -> None:
        self._ws = ws
        self._config = config
        self._transcript_queue: asyncio.Queue[Transcript] = asyncio.Queue()
        self._closed = False
        self._listen_task: asyncio.Task | None = None

    async def start_listening(self) -> None:
        self._listen_task = asyncio.create_task(self._listen_loop())

    async def _listen_loop(self) -> None:
        try:
            async for msg in self._ws:
                if self._closed:
                    break
                if hasattr(msg, "data"):
                    data = msg.data if isinstance(msg.data, str) else msg.data.decode()
                else:
                    data = str(msg)

                try:
                    parsed = json.loads(data)
                except json.JSONDecodeError:
                    continue

                msg_type = parsed.get("type", "")

                if msg_type == "Results":
                    channel = parsed.get("channel", {})
                    alternatives = channel.get("alternatives", [])
                    if not alternatives:
                        continue

                    alt = alternatives[0]
                    text = alt.get("transcript", "").strip()
                    if not text:
                        continue

                    words = [
                        TranscriptWord(
                            word=w.get("word", ""),
                            start=w.get("start", 0.0),
                            end=w.get("end", 0.0),
                            confidence=w.get("confidence", 0.0),
                        )
                        for w in alt.get("words", [])
                    ]

                    transcript = Transcript(
                        text=text,
                        is_final=parsed.get("is_final", False),
                        confidence=alt.get("confidence", 0.0),
                        words=words,
                        speech_final=parsed.get("speech_final", False),
                    )
                    await self._transcript_queue.put(transcript)

                elif msg_type == "UtteranceEnd":
                    await self._transcript_queue.put(
                        Transcript(text="", is_final=True, speech_final=True)
                    )

        except Exception:
            if not self._closed:
                logger.exception("Deepgram listen loop error")

    async def send_audio(self, audio_bytes: bytes) -> None:
        if self._closed:
            return
        try:
            await self._ws.send(audio_bytes)
        except Exception:
            if not self._closed:
                logger.exception("Failed to send audio to Deepgram")

    async def receive_transcripts(self) -> AsyncIterator[Transcript]:
        while not self._closed:
            try:
                transcript = await asyncio.wait_for(
                    self._transcript_queue.get(), timeout=0.1,
                )
                yield transcript
            except asyncio.TimeoutError:
                continue

    async def close(self) -> None:
        self._closed = True
        if self._listen_task:
            self._listen_task.cancel()
        try:
            await self._ws.close()
        except Exception:
            pass


class DeepgramSTT(STTProvider):
    """Deepgram streaming STT provider using WebSocket API."""

    def __init__(self, api_key: str, base_url: str = "wss://api.deepgram.com") -> None:
        self._api_key = api_key
        self._base_url = base_url

    async def start_stream(self, config: STTConfig | None = None) -> STTStream:
        cfg = config or STTConfig()

        try:
            import websockets
        except ImportError as e:
            raise ImportError(
                "websockets is required for Deepgram STT. "
                "Install with: uv pip install websockets"
            ) from e

        params = (
            f"model={cfg.model}&language={cfg.language}"
            f"&sample_rate={cfg.sample_rate}&channels={cfg.channels}"
            f"&encoding={cfg.encoding}&smart_format={'true' if cfg.smart_format else 'false'}"
            f"&interim_results={'true' if cfg.interim_results else 'false'}"
            f"&endpointing={cfg.endpointing}"
            f"&utterance_end_ms={cfg.utterance_end_ms}"
            f"&vad_events={'true' if cfg.vad_events else 'false'}"
        )

        url = f"{self._base_url}/v1/listen?{params}"
        headers = {"Authorization": f"Token {self._api_key}"}

        ws = await websockets.connect(url, additional_headers=headers)
        stream = DeepgramSTTStream(ws, cfg)
        await stream.start_listening()

        logger.info("Deepgram STT stream started (model=%s, lang=%s)", cfg.model, cfg.language)
        return stream
