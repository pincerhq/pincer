"""
Signal REST client for signal-cli-rest-api.

Wraps the HTTP/WebSocket API exposed by the bbernhard/signal-cli-rest-api
Docker image. Supports both normal and json-rpc modes:

    docker compose -f docker-compose.yml -f docker-compose.signal.yml up -d
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


class SignalAPIError(Exception):
    """Raised when signal-cli-rest-api returns an error."""


@dataclass
class SignalAttachment:
    id: str = ""
    content_type: str = "application/octet-stream"
    filename: str = ""
    size: int = 0


@dataclass
class SignalMessage:
    """Parsed inbound Signal envelope."""

    source: str = ""
    source_name: str = ""
    timestamp: int = 0
    text: str = ""
    group_id: str = ""
    is_group: bool = False
    attachments: list[SignalAttachment] = field(default_factory=list)
    has_voice: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


class SignalClient:
    """Async HTTP + WebSocket client for signal-cli-rest-api."""

    def __init__(self, base_url: str, phone_number: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._phone = phone_number
        self._session: Any = None  # aiohttp.ClientSession

    async def connect(self) -> None:
        import aiohttp

        self._session = aiohttp.ClientSession()

    async def disconnect(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    def _ensure_session(self) -> Any:
        if not self._session:
            raise SignalAPIError("SignalClient not connected — call connect() first")
        return self._session

    async def health(self) -> dict[str, Any]:
        """GET /v1/health"""
        session = self._ensure_session()
        async with session.get(f"{self._base_url}/v1/health") as resp:
            if resp.status != 200:
                raise SignalAPIError(f"Health check failed: {resp.status}")
            return await resp.json()

    async def about(self) -> dict[str, Any]:
        """GET /v1/about"""
        session = self._ensure_session()
        async with session.get(f"{self._base_url}/v1/about") as resp:
            return await resp.json()

    async def list_accounts(self) -> list[str]:
        """GET /v1/accounts — returns registered phone numbers."""
        session = self._ensure_session()
        async with session.get(f"{self._base_url}/v1/accounts") as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            if isinstance(data, list):
                return [str(a) for a in data]
            return []

    async def get_qr_link(self, device_name: str = "Pincer") -> str:
        """Return the QR-code link URL for device registration."""
        return f"{self._base_url}/v1/qrcodelink?device_name={device_name}"

    async def receive(self) -> list[SignalMessage]:
        """GET /v1/receive/{number} — poll for new messages (non-WS mode)."""
        session = self._ensure_session()
        url = f"{self._base_url}/v1/receive/{self._phone}"
        async with session.get(url) as resp:
            if resp.status != 200:
                raise SignalAPIError(f"Receive failed: {resp.status}")
            data = await resp.json()
            if not isinstance(data, list):
                return []
            return [self._parse_message(env) for env in data if self._is_data_message(env)]

    async def send_message(
        self, recipient: str, message: str, *, attachments: list[str] | None = None
    ) -> None:
        """POST /v2/send — send a DM."""
        session = self._ensure_session()
        payload: dict[str, Any] = {
            "message": message,
            "number": self._phone,
            "recipients": [recipient],
        }
        if attachments:
            payload["base64_attachments"] = attachments
        async with session.post(f"{self._base_url}/v2/send", json=payload) as resp:
            if resp.status not in (200, 201):
                body = await resp.text()
                raise SignalAPIError(f"send_message failed ({resp.status}): {body}")

    async def send_group_message(self, group_id: str, message: str) -> None:
        """POST /v1/send/group — send to a Signal group."""
        session = self._ensure_session()
        payload: dict[str, Any] = {
            "message": message,
            "number": self._phone,
            "group_id": group_id,
        }
        async with session.post(f"{self._base_url}/v1/send/group", json=payload) as resp:
            if resp.status not in (200, 201):
                body = await resp.text()
                raise SignalAPIError(f"send_group_message failed ({resp.status}): {body}")

    async def send_reaction(self, recipient: str, emoji: str, target_timestamp: int) -> None:
        """PUT /v1/reactions/{number}"""
        session = self._ensure_session()
        payload = {
            "recipient": recipient,
            "emoji": emoji,
            "target_author": recipient,
            "timestamp": target_timestamp,
        }
        async with session.put(
            f"{self._base_url}/v1/reactions/{self._phone}", json=payload
        ) as resp:
            if resp.status not in (200, 201, 204):
                logger.debug("send_reaction status %s", resp.status)

    async def send_typing_indicator(self, recipient: str) -> None:
        """PUT /v1/typing-indicator/{number}"""
        session = self._ensure_session()
        payload = {"recipient": recipient}
        async with session.put(
            f"{self._base_url}/v1/typing-indicator/{self._phone}", json=payload
        ) as resp:
            if resp.status not in (200, 201, 204):
                logger.debug("send_typing_indicator status %s", resp.status)

    async def list_groups(self) -> list[dict[str, Any]]:
        """GET /v1/groups/{number}"""
        session = self._ensure_session()
        async with session.get(f"{self._base_url}/v1/groups/{self._phone}") as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            return data if isinstance(data, list) else []

    async def get_attachment(self, attachment_id: str) -> bytes:
        """GET /v1/attachments/{id} — download raw attachment bytes."""
        session = self._ensure_session()
        async with session.get(
            f"{self._base_url}/v1/attachments/{attachment_id}"
        ) as resp:
            if resp.status != 200:
                raise SignalAPIError(f"Attachment download failed: {resp.status}")
            return await resp.read()

    # ── WebSocket receive ─────────────────────────────────────────────────────

    async def websocket_receive(
        self,
    ):  # type: ignore[return]
        """Async generator yielding SignalMessage via WebSocket.

        Usage::

            async for msg in client.websocket_receive():
                ...
        """
        import aiohttp

        ws_url = (
            self._base_url.replace("http://", "ws://")
            .replace("https://", "wss://")
        )
        ws_url = f"{ws_url}/v1/receive/{self._phone}"
        session = self._ensure_session()
        try:
            async with session.ws_connect(ws_url) as ws:
                async for raw_msg in ws:
                    if raw_msg.type == aiohttp.WSMsgType.TEXT:
                        import json

                        try:
                            envelope = json.loads(raw_msg.data)
                        except Exception:
                            continue
                        if self._is_data_message(envelope):
                            yield self._parse_message(envelope)
                    elif raw_msg.type in (
                        aiohttp.WSMsgType.ERROR,
                        aiohttp.WSMsgType.CLOSED,
                    ):
                        break
        except Exception as exc:
            raise SignalAPIError(f"WebSocket error: {exc}") from exc

    # ── Parsing ───────────────────────────────────────────────────────────────

    @staticmethod
    def _is_data_message(envelope: dict[str, Any]) -> bool:
        """Return True if envelope contains a text/attachment data message."""
        env = envelope.get("envelope", envelope)
        dm = env.get("dataMessage")
        sync_dm = env.get("syncMessage", {}).get("sentMessage")
        return bool(dm) or bool(sync_dm)

    @classmethod
    def _parse_message(cls, envelope: dict[str, Any]) -> SignalMessage:
        """Extract SignalMessage from a raw envelope dict."""
        env = envelope.get("envelope", envelope)
        source: str = env.get("source", "") or env.get("sourceNumber", "")
        source_name: str = env.get("sourceName", "")
        timestamp: int = env.get("timestamp", 0)

        dm = env.get("dataMessage") or env.get("syncMessage", {}).get("sentMessage", {})
        if not dm:
            dm = {}

        text: str = dm.get("message", "") or ""
        group_info = dm.get("groupInfo") or dm.get("groupContext") or {}
        group_id: str = group_info.get("groupId", "")
        is_group = bool(group_id)

        raw_atts = dm.get("attachments", []) or []
        attachments: list[SignalAttachment] = []
        has_voice = False
        for att in raw_atts:
            att_id = (
                att.get("id")
                or att.get("uploadTimestamp")
                or str(att.get("size", ""))
            )
            content_type = att.get("contentType", "application/octet-stream")
            if content_type.startswith("audio/"):
                has_voice = True
            attachments.append(
                SignalAttachment(
                    id=str(att_id),
                    content_type=content_type,
                    filename=att.get("filename", ""),
                    size=att.get("size", 0),
                )
            )

        return SignalMessage(
            source=source,
            source_name=source_name,
            timestamp=timestamp,
            text=text,
            group_id=group_id,
            is_group=is_group,
            attachments=attachments,
            has_voice=has_voice,
            raw=envelope,
        )
