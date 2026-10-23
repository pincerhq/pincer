"""In-loop stand-ins for what the voice engines talk to: the Twilio WebSocket,
the Twilio REST API, the Deepgram stream and the ElevenLabs provider.

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
from types import SimpleNamespace
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


class _NoHttpClient:
    """Stands in for AsyncTwilioHttpClient, whose constructor opens an aiohttp
    session the fake Client would never use or close."""

    session = None


def install_fake_twilio(monkeypatch: Any, client: Any) -> None:
    """Make TwilioRest build ``client`` (a Client class or factory) instead of
    the real twilio Client."""
    monkeypatch.setattr("twilio.rest.Client", client)
    monkeypatch.setattr("twilio.http.async_http_client.AsyncTwilioHttpClient", _NoHttpClient)


class FakeTwilio:
    """Twilio REST Client with the async calls TwilioRest makes. Records every
    request as (op, params); clear `release` to hold requests in flight, the
    way a slow api.twilio.com round-trip does."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args, self.kwargs = args, kwargs
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.release = asyncio.Event()
        self.release.set()
        self.calls = _FakeTwilioCalls(self)

    async def request(self, op: str, params: dict[str, Any]) -> Any:
        self.requests.append((op, params))
        await self.release.wait()
        return SimpleNamespace(sid="CA_new")


class _FakeTwilioCalls:
    def __init__(self, client: FakeTwilio) -> None:
        self._client = client

    def __call__(self, sid: str) -> Any:
        return SimpleNamespace(update_async=lambda **params: self._client.request("update", {"sid": sid, **params}))

    def create_async(self, **params: Any) -> Any:
        return self._client.request("create", params)


class FakeTwilioRest:
    """Stands in for pincer.voice.twilio_rest.TwilioRest one level up from
    FakeTwilio: for tests about what the engine does around a REST call, not
    about the façade itself. Records calls; `hold` makes them slow, `raise_on`
    makes them fail."""

    def __init__(self) -> None:
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.creates: list[dict[str, Any]] = []
        self.hold: asyncio.Event | None = None  # when set, every call awaits it first
        self.raise_on: set[str] = set()  # {"update_call", "create_call"}
        self.in_flight = 0
        self.next_sid = "CA_fake"
        self.closed = False

    async def _gate(self, name: str) -> None:
        self.in_flight += 1
        try:
            if self.hold is not None:
                await self.hold.wait()
        finally:
            self.in_flight -= 1
        if name in self.raise_on:
            raise RuntimeError(f"fake twilio {name} failed")

    async def update_call(self, call_sid: str, *, twiml: str | None = None, status: str | None = None) -> Any:
        await self._gate("update_call")
        self.updates.append((call_sid, {"twiml": twiml, "status": status}))
        return SimpleNamespace(sid=call_sid)

    async def create_call(self, **kwargs: Any) -> Any:
        await self._gate("create_call")
        self.creates.append(kwargs)
        return SimpleNamespace(sid=self.next_sid)

    async def aclose(self) -> None:
        self.closed = True


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
