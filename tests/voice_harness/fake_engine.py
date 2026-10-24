"""In-process VoiceEngine for the reliability harness — no Twilio, no PSTN."""

from __future__ import annotations

import asyncio
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
        if getattr(self, "transfer_raises", False):
            raise RuntimeError("dial failed")

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
