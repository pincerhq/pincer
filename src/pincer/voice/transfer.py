"""
Warm transfer — agent calls provider, navigates to the right person,
then patches the user in via Twilio Conference bridge.

Flow:
1. Agent places outbound call to provider
2. Agent navigates IVR / waits on hold
3. When human answers, agent confirms readiness
4. Agent creates a conference room and adds both parties
5. Agent optionally stays on or disconnects
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pincer.config import Settings

logger = logging.getLogger(__name__)


class TransferStatus(StrEnum):
    PENDING = "pending"
    CALLING_PROVIDER = "calling_provider"
    NAVIGATING_IVR = "navigating_ivr"
    ON_HOLD = "on_hold"
    PROVIDER_CONNECTED = "provider_connected"
    CONFERENCE_ACTIVE = "conference_active"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class TransferState:
    user_call_sid: str
    provider_call_sid: str = ""
    conference_sid: str = ""
    provider_number: str = ""
    topic: str = ""
    status: TransferStatus = TransferStatus.PENDING
    user_name: str = ""


class WarmTransfer:
    """Manages warm transfer flow using Twilio Conference API."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._active_transfers: dict[str, TransferState] = {}

    async def initiate(
        self,
        user_call_sid: str,
        provider_number: str,
        topic: str,
        user_name: str = "",
    ) -> TransferState:
        """Start a warm transfer by calling the provider."""
        state = TransferState(
            user_call_sid=user_call_sid,
            provider_number=provider_number,
            topic=topic,
            user_name=user_name,
            status=TransferStatus.CALLING_PROVIDER,
        )

        try:
            from twilio.rest import Client

            client = Client(
                self._settings.twilio_account_sid,
                self._settings.twilio_auth_token.get_secret_value(),
            )

            base_url = self._settings.voice_webhook_base_url.strip().rstrip("/")
            host = base_url
            for prefix in ("https://", "http://"):
                if host.startswith(prefix):
                    host = host[len(prefix) :]
                    break
            host = host.rstrip("/")

            stream_url = f"wss://{host}/voice/stream/{{CallSid}}"
            twiml = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<Response>"
                f'<Connect><Stream url="{stream_url}" /></Connect>'
                "</Response>"
            )

            call = client.calls.create(
                to=provider_number,
                from_=self._settings.twilio_phone_number,
                twiml=twiml,
                status_callback=f"{base_url}/voice/status",
                status_callback_event=["ringing", "answered", "completed"],
                timeout=30,
                time_limit=self._settings.voice_max_call_duration,
            )

            state.provider_call_sid = call.sid
            self._active_transfers[user_call_sid] = state

            logger.info(
                "Warm transfer initiated: user=%s -> provider=%s (call=%s)",
                user_call_sid,
                provider_number,
                call.sid,
            )

        except ImportError:
            state.status = TransferStatus.FAILED
            logger.error("Twilio SDK not installed for warm transfer")
        except Exception:
            state.status = TransferStatus.FAILED
            logger.exception("Failed to initiate warm transfer")

        return state

    async def bridge(self, user_call_sid: str) -> bool:
        """Create conference bridge between user and provider."""
        state = self._active_transfers.get(user_call_sid)
        if not state:
            logger.warning("No active transfer for %s", user_call_sid)
            return False

        try:
            from twilio.rest import Client

            client = Client(
                self._settings.twilio_account_sid,
                self._settings.twilio_auth_token.get_secret_value(),
            )

            conf_name = f"transfer_{user_call_sid}"

            user_twiml = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<Response>"
                f"<Dial><Conference>{conf_name}</Conference></Dial>"
                "</Response>"
            )
            client.calls(user_call_sid).update(twiml=user_twiml)

            provider_twiml = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<Response>"
                f"<Dial><Conference>{conf_name}</Conference></Dial>"
                "</Response>"
            )
            client.calls(state.provider_call_sid).update(twiml=provider_twiml)

            state.status = TransferStatus.CONFERENCE_ACTIVE
            logger.info("Conference bridge active: %s", conf_name)
            return True

        except Exception:
            state.status = TransferStatus.FAILED
            logger.exception("Failed to bridge transfer for %s", user_call_sid)
            return False

    async def end_transfer(self, user_call_sid: str) -> None:
        """Clean up a completed or failed transfer."""
        state = self._active_transfers.pop(user_call_sid, None)
        if state:
            state.status = TransferStatus.COMPLETED
            logger.info("Transfer ended: %s", user_call_sid)

    def get_transfer_state(self, user_call_sid: str) -> TransferState | None:
        return self._active_transfers.get(user_call_sid)
