"""In-process VoiceEngine for the reliability harness — no Twilio, no PSTN."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from pincer.voice.engine import CallDirection, CallState, VoiceEngine


class FakeVoiceEngine(VoiceEngine):
    """Records agent speech; call teardown mirrors the real engines
    (unregister + on_call_end callback) without touching Twilio."""

    def __init__(self, settings: Any) -> None:
        super().__init__(settings)
        self.spoken: dict[str, list[str]] = {}
        self.sent_flags: dict[str, list[tuple[str, bool]]] = {}  # (text, last) per send
        self.interrupts: dict[str, int] = {}
        # by_caller=False silences: how many lines had been spoken at each one
        self.silenced: dict[str, list[int]] = {}
        self.dtmf: dict[str, list[str]] = {}
        self.ended: list[str] = []
        self.transfers: dict[str, list[dict[str, Any]]] = {}  # Sprint 12: <Dial> redirects
        # what Twilio reports for a call (fetch_call_status); an exception is raised
        self.call_status: str | Exception = "in-progress"
        self.status_fetches: list[str] = []
        # the <Dial> redirect: transfer_raises=True is a definite Twilio error,
        # an exception instance is raised as is; redirect_hook(call_sid) runs
        # while the update is "in flight" (e.g. the caller hangs up)
        self.transfer_raises: bool | BaseException = False
        self.redirect_hook: Callable[[str], None] | None = None
        # set: the update waits for it (Twilio slow to answer)
        self.redirect_gate: asyncio.Event | None = None
        # The <Dial> redirect obeys the hooks above; every other REST call goes
        # to the engine's own TwilioRest (a test's fake, when it installs one).
        self._rest = _HookedRedirect(self, self._rest)
        self.wait_for_turns = True
        #: When set, send_speech holds the agent "mid-sentence" until released;
        #: `speech_waiting` fires once something is held.
        self.speech_blocker: asyncio.Event | None = None
        self.speech_waiting = asyncio.Event()

    @property
    def engine_name(self) -> str:
        return "fake"

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
        self.spoken.setdefault(call_sid, [])
        return await self._register_call(
            call_sid, caller, direction, target_number, target_name, purpose, language, instructions
        )

    async def on_speech_input(self, call_sid: str, text_or_audio: Any) -> None:
        if self._on_speech_callback:
            dispatched = await self._on_speech_callback(call_sid, str(text_or_audio))
            # The real transports hand the utterance off and keep reading; a
            # scripted conversation waits for each reply instead. Barge-in
            # tests switch this off to get the production concurrency.
            # A turn cancelled by call end or barge-in is finished, not failed.
            if self.wait_for_turns and isinstance(dispatched, asyncio.Task):
                await asyncio.wait([dispatched])
                if not dispatched.cancelled() and dispatched.exception() is not None:
                    raise dispatched.exception()  # type: ignore[misc]

    async def send_speech(self, call_sid: str, text_or_audio: Any, *, last: bool = True) -> bool:
        text = str(text_or_audio)
        if self.speech_blocker is not None and text:
            self.speech_waiting.set()
            await self.speech_blocker.wait()
        if text:  # empty last=True closer (CR stream close) is not "speech"
            self.spoken.setdefault(call_sid, []).append(text)
        self.sent_flags.setdefault(call_sid, []).append((text, last))
        return True

    async def interrupt_speech(self, call_sid: str, *, by_caller: bool = True) -> None:
        if not by_caller:
            self.silenced.setdefault(call_sid, []).append(len(self.spoken.get(call_sid, [])))
            return
        self.interrupts[call_sid] = self.interrupts.get(call_sid, 0) + 1

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
        self.transfers.setdefault(call_sid, []).append(
            {
                "target": target_number,
                "timeout_s": timeout_s,
                "action_url": action_url,
                "announce": announce,
                "language": language,
            }
        )
        # the real bookkeeping (transferring flag, a fresh `transfer` record per
        # dial, rollback on failure); only the Twilio redirect is faked
        await super().transfer_call(
            call_sid, target_number, timeout_s=timeout_s, action_url=action_url, announce=announce, language=language
        )

    async def fetch_call_status(self, call_sid: str) -> str:
        self.status_fetches.append(call_sid)
        if isinstance(self.call_status, Exception):
            raise self.call_status
        return self.call_status

    async def end_call(self, call_sid: str) -> None:
        if call_sid in self.ended:
            return
        self.ended.append(call_sid)
        state = await self._unregister_call(call_sid)
        if state and self._on_call_end_callback:
            await self._on_call_end_callback(call_sid, state)

    async def send_dtmf(self, call_sid: str, digits: str) -> None:
        self.dtmf.setdefault(call_sid, []).append(digits)

    async def close_media_stream(self, call_sid: str) -> None:
        pass


class _HookedRedirect:
    """The engine's TwilioRest with its call update (the <Dial> redirect; the
    fake ends calls and sends DTMF itself) driven by the fake engine's
    `transfer_raises` / `redirect_hook` / `redirect_gate`."""

    def __init__(self, engine: FakeVoiceEngine, rest: Any) -> None:
        object.__setattr__(self, "_engine", engine)
        object.__setattr__(self, "_rest", rest)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._rest, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._rest, name, value)  # e.g. a test swapping in its Twilio client

    async def update_call(self, call_sid: str, **_params: Any) -> None:
        engine = self._engine
        if engine.redirect_hook is not None:
            engine.redirect_hook(call_sid)
        if engine.redirect_gate is not None:
            await engine.redirect_gate.wait()
        if isinstance(engine.transfer_raises, BaseException):
            raise engine.transfer_raises
        if engine.transfer_raises:
            from twilio.base.exceptions import TwilioRestException

            # a definite Twilio error: the redirect was not applied
            raise TwilioRestException(500, f"/Calls/{call_sid}", "dial failed")
