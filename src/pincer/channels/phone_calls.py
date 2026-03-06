"""
Voice channel — phone calls as a first-class Pincer channel.

Implements BaseChannel for voice, mapping Twilio calls to the same
messaging abstraction used by Telegram, WhatsApp, and Discord.
Voice sessions share memory with text channels via cross-channel identity.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pincer.channels.base import BaseChannel, ChannelType, IncomingMessage, MessageHandler

if TYPE_CHECKING:
    import asyncio

    from pincer.config import Settings
    from pincer.core.identity import IdentityResolver
    from pincer.voice.engine import CallState, VoiceEngine
    from pincer.voice.state_machine import CallStateMachine

logger = logging.getLogger(__name__)


class VoiceChannel(BaseChannel):
    """Phone call channel — bridges Twilio voice to the Pincer agent."""

    channel_type = ChannelType.VOICE

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._handler: MessageHandler | None = None
        self._engine: VoiceEngine | None = None
        self._identity: IdentityResolver | None = None
        self._state_machines: dict[str, CallStateMachine] = {}
        self._response_queues: dict[str, asyncio.Queue[str]] = {}

    @property
    def name(self) -> str:
        return "voice"

    def set_engine(self, engine: VoiceEngine) -> None:
        self._engine = engine
        engine.set_on_speech(self._handle_speech)
        engine.set_on_call_end(self._handle_call_end)

    def set_identity_resolver(self, identity: IdentityResolver) -> None:
        self._identity = identity

    async def start(self, handler: MessageHandler) -> None:
        self._handler = handler
        logger.info("Voice channel started")

    async def stop(self) -> None:
        if self._engine:
            for call_sid in list(self._engine.get_active_calls()):
                try:
                    await self._engine.end_call(call_sid)
                except Exception:
                    logger.exception("Error ending call %s during shutdown", call_sid)
        logger.info("Voice channel stopped")

    async def send(self, user_id: str, text: str, **kwargs: Any) -> None:
        """Send a text response to the active voice call for this user.

        In voice mode, the engine converts the text to speech.
        """
        call_sid = kwargs.get("call_sid", "")
        if not call_sid:
            call_sid = self._find_active_call_for_user(user_id)

        if call_sid and self._engine:
            await self._engine.send_speech(call_sid, text)
        else:
            logger.warning("No active call for user %s to send speech", user_id)

    async def _handle_speech(self, call_sid: str, text: str) -> None:
        """Called when the caller speaks (STT output or ConversationRelay text)."""
        if not self._handler:
            return

        state = self._engine.get_call_state(call_sid) if self._engine else None
        if not state:
            return

        user_id = state.caller_number
        pincer_user_id = ""

        if self._identity:
            try:
                pincer_user_id = await self._identity.resolve(
                    ChannelType.VOICE, state.caller_number,
                )
            except Exception:
                logger.debug("Could not resolve identity for %s", state.caller_number)

        incoming = IncomingMessage(
            user_id=user_id,
            channel="voice",
            text=text,
            pincer_user_id=pincer_user_id,
            channel_type=ChannelType.VOICE,
        )

        try:
            response = await self._handler(incoming)
            if response and self._engine:
                await self._engine.send_speech(call_sid, response)
        except Exception:
            logger.exception("Error handling voice input for call %s", call_sid)
            if self._engine:
                await self._engine.send_speech(
                    call_sid,
                    "I'm sorry, I had trouble processing that. Could you say that again?",
                )

    async def _handle_call_end(self, call_sid: str, state: CallState) -> None:
        """Called when a call ends — cleanup and send post-call summary."""
        self._state_machines.pop(call_sid, None)
        self._response_queues.pop(call_sid, None)
        logger.info(
            "Call ended: %s (%s, %ds)",
            call_sid, state.direction, state.duration_seconds,
        )

    def _find_active_call_for_user(self, user_id: str) -> str:
        """Find the active call SID for a given user."""
        if not self._engine:
            return ""
        for call_sid, state in self._engine.get_active_calls().items():
            if state.caller_number == user_id or state.user_id == user_id:
                return call_sid
        return ""

    def get_state_machine(self, call_sid: str) -> CallStateMachine | None:
        return self._state_machines.get(call_sid)

    def set_state_machine(self, call_sid: str, sm: CallStateMachine) -> None:
        self._state_machines[call_sid] = sm
