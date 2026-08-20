"""In-process VoiceEngine for the reliability harness — no Twilio, no PSTN."""

from __future__ import annotations

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
        self.dtmf: dict[str, list[str]] = {}
        self.ended: list[str] = []
        self.transfers: dict[str, list[dict[str, Any]]] = {}  # Sprint 12: <Dial> redirects

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
            await self._on_speech_callback(call_sid, str(text_or_audio))

    async def send_speech(self, call_sid: str, text_or_audio: Any, *, last: bool = True) -> bool:
        text = str(text_or_audio)
        if text:  # empty last=True closer (CR stream close) is not "speech"
            self.spoken.setdefault(call_sid, []).append(text)
        self.sent_flags.setdefault(call_sid, []).append((text, last))
        return True

    async def interrupt_speech(self, call_sid: str) -> None:
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
