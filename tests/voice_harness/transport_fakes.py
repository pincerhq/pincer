"""In-loop stand-ins for what the voice engines talk to: the Twilio WebSocket,
the Deepgram stream and the ElevenLabs provider.

They exist for the barge-in regressions, which have to drive the REAL read
loops (`relay_ws`, the Media Streams transcript consumer). Synchronisation is
by events, never by sleeping: `await sock.inbox.join()` / `await
stt.queue.join()` returns once the loop has finished acting on everything fed
to it (the next read only happens after the previous frame was handled), and
`await sock.wait_sent(pred)` once a matching frame has been written.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from starlette.websockets import WebSocketDisconnect

if TYPE_CHECKING:
    from collections.abc import Callable

    from pincer.voice.stt import Transcript


class FakeSocket:
    """Twilio WebSocket: inbound frames from a queue (None = Twilio hangs up),
    outbound frames recorded as parsed JSON."""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self._sent_changed = asyncio.Condition()
        self._reading = False

    async def accept(self) -> None: ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...

    async def receive_text(self) -> str:
        if self._reading:
            self.inbox.task_done()  # the previous frame has been fully handled
        frame = await self.inbox.get()
        self._reading = True
        if frame is None:
            self.inbox.task_done()
            raise WebSocketDisconnect()
        return frame

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))
        async with self._sent_changed:
            self._sent_changed.notify_all()

    def push(self, **frame: Any) -> None:
        self.inbox.put_nowait(json.dumps(frame))

    def hang_up(self) -> None:
        self.inbox.put_nowait(None)

    async def wait_sent(self, predicate: Callable[[list[dict[str, Any]]], bool], timeout: float = 2.0) -> None:
        async with asyncio.timeout(timeout), self._sent_changed:
            await self._sent_changed.wait_for(lambda: predicate(self.sent))

    def events(self, event: str) -> list[dict[str, Any]]:
        """Media Streams frames of one kind (`media`, `clear`)."""
        return [m for m in self.sent if m.get("event") == event]

    def tokens(self) -> list[str]:
        """ConversationRelay text tokens, empty closers left out."""
        return [m["token"] for m in self.sent if m.get("type") == "text" and m.get("token")]


class FakeSTTStream:
    """Deepgram stream: the test feeds transcripts; `queue.join()` waits until
    the consumer has acted on all of them."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[Transcript] = asyncio.Queue()
        self.closed = False

    async def send_audio(self, audio_bytes: bytes) -> None: ...

    async def receive_transcripts(self):
        while not self.closed:
            transcript = await self.queue.get()
            yield transcript
            self.queue.task_done()  # resumed = the consumer finished with it

    async def close(self) -> None:
        self.closed = True


class FakeSTT:
    def __init__(self) -> None:
        self.streams: list[FakeSTTStream] = []

    async def start_stream(self, config: Any = None) -> FakeSTTStream:
        self.streams.append(FakeSTTStream())
        return self.streams[-1]


class FakeTTS:
    """One second of μ-law per chunk, written faster than real time — so the
    agent is still audible at Twilio while the turn waits on the LLM.

    `cancel()` behaves like the real provider's: one flag for every stream.
    """

    output_format = "ulaw_8000"

    def __init__(self, chunks: int = 2, delay_s: float = 0.0) -> None:
        self.chunks = chunks
        self.delay_s = delay_s
        self.synthesized: list[str] = []
        self.started = asyncio.Event()
        self._cancelled = False
        #: When set, the FIRST synthesis waits on it before producing audio.
        self.hold_first: asyncio.Event | None = None

    async def synthesize_stream(self, text: str, voice: Any = None, model: Any = None):
        self._cancelled = False
        self.synthesized.append(text)
        self.started.set()
        if self.hold_first is not None and len(self.synthesized) == 1:
            await self.hold_first.wait()
        for _ in range(self.chunks):
            if self._cancelled:
                return
            if self.delay_s:
                await asyncio.sleep(self.delay_s)  # synthesis time, not test synchronisation
            yield b"\xff" * 8000

    async def cancel(self) -> None:
        self._cancelled = True
