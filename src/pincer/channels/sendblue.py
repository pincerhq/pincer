"""Direct iMessage/SMS via Sendblue, with an authenticated, bounded webhook inbox."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import re
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

import httpx

from pincer.channels.base import BaseChannel, ChannelType, IncomingMessage

if TYPE_CHECKING:
    from aiohttp import web

    from pincer.channels.base import MessageHandler
    from pincer.config import Settings
    from pincer.core.identity import IdentityResolver

logger = logging.getLogger(__name__)
_PHONE = re.compile(r"\+[1-9][0-9]{7,14}\Z")
_MEDIA_NOTICE = "[Attachment received; this channel supports text only. Please resend its contents as text.]"


class SendblueChannel(BaseChannel):
    """Queue authenticated direct messages and send the agent's final text reply.

    The inbox and duplicate cache are process-local. Acknowledgement means queued,
    not durable or delivered. Outbound POSTs are never retried automatically.
    """

    channel_type = ChannelType.SENDBLUE

    def __init__(self, settings: Settings, identity: IdentityResolver | None = None) -> None:
        self._settings = settings
        self._identity = identity
        self._handler: MessageHandler | None = None
        self._client: httpx.AsyncClient | None = None
        self._runner: web.AppRunner | None = None
        self._worker: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[IncomingMessage] = asyncio.Queue(maxsize=128)
        self._seen: OrderedDict[str, None] = OrderedDict()

    @property
    def name(self) -> str:
        return "sendblue"

    async def start(self, handler: MessageHandler) -> None:
        from aiohttp import web

        cfg = self._settings
        if not all(
            (
                cfg.sendblue_api_key.get_secret_value(),
                cfg.sendblue_api_secret.get_secret_value(),
                cfg.sendblue_signing_secret.get_secret_value(),
                _PHONE.fullmatch(cfg.sendblue_from_number),
            )
        ):
            raise ValueError("Sendblue requires API credentials, a signing secret, an E.164 line")
        if any(number != "*" and not _PHONE.fullmatch(number) for number in cfg.sendblue_allow_from):
            raise ValueError("Sendblue allowlist entries must be E.164 numbers or explicit '*'")
        self._handler = handler
        self._client = httpx.AsyncClient(
            base_url="https://api.sendblue.com",
            headers={
                "sb-api-key-id": cfg.sendblue_api_key.get_secret_value(),
                "sb-api-secret-key": cfg.sendblue_api_secret.get_secret_value(),
            },
            timeout=30,
            follow_redirects=False,
        )
        app = web.Application(client_max_size=65536)
        app.router.add_post("/webhooks/sendblue", self._receive)
        self._runner = web.AppRunner(app, access_log=None)
        try:
            await self._runner.setup()
            await web.TCPSite(self._runner, cfg.sendblue_webhook_host, cfg.sendblue_webhook_port).start()
            self._worker = asyncio.create_task(self._process())
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        if self._worker:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._queue.join(), timeout=30)
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _receive(self, request: web.Request) -> web.Response:
        from aiohttp import web

        supplied = request.headers.get("sb-signing-secret", "").encode("utf-8", "surrogateescape")
        expected = self._settings.sendblue_signing_secret.get_secret_value().encode()
        if not expected or not hmac.compare_digest(supplied, expected):
            return web.Response(status=401)
        try:
            payload = json.loads(await request.read())
        except (ValueError, UnicodeDecodeError):
            return web.Response(status=400)
        if not isinstance(payload, dict):
            return web.Response(status=400)
        if (
            payload.get("is_outbound") is not False
            or payload.get("status") != "RECEIVED"
            or payload.get("group_id") not in (None, "")
            or payload.get("to_number") != self._settings.sendblue_from_number
        ):
            return web.Response(status=200)
        sender, handle = payload.get("from_number"), payload.get("message_handle")
        content, media = payload.get("content"), payload.get("media_url")
        if (
            not isinstance(sender, str)
            or not _PHONE.fullmatch(sender)
            or not isinstance(handle, str)
            or not 0 < len(handle) <= 256
            or (content is not None and not isinstance(content, str))
            or (media is not None and not isinstance(media, str))
        ):
            return web.Response(status=400)
        allowed = self._settings.sendblue_allow_from
        if sender not in allowed and "*" not in allowed:
            return web.Response(status=200)
        if self._identity and self._identity.has_config and await self._identity.is_guest(self.channel_type, sender):
            return web.Response(status=200)
        if handle in self._seen:
            return web.Response(status=200)
        text = content or ""
        if media:
            text = f"{text}\n{_MEDIA_NOTICE}".strip()
        if not text.strip():
            return web.Response(status=200)
        try:
            self._queue.put_nowait(
                IncomingMessage(
                    user_id=sender,
                    channel=self.name,
                    channel_type=self.channel_type,
                    text=text,
                    reply_to_message_id=handle,
                )
            )
        except asyncio.QueueFull:
            return web.Response(status=503)
        self._seen[handle] = None
        if len(self._seen) > 4096:
            self._seen.popitem(last=False)
        return web.Response(status=200)

    async def _process(self) -> None:
        while True:
            message = await self._queue.get()
            try:
                if self._handler is None:
                    raise RuntimeError("Sendblue handler not initialized")
                reply = await self._handler(message)
                if reply:
                    await self.send(message.user_id, reply)
            except Exception:
                # Provider exceptions can contain message text or credentials. Log only
                # the failure and correlation handle; inspect delivery in Sendblue.
                logger.error(
                    "Sendblue processing failed for message %s; no automatic replay", message.reply_to_message_id
                )
            finally:
                self._queue.task_done()

    async def send(self, user_id: str, text: str, **kwargs: Any) -> None:
        if not self._client:
            raise RuntimeError("Sendblue channel is not started")
        if not _PHONE.fullmatch(user_id):
            raise ValueError("Sendblue recipient must be an E.164 number")
        allowed = self._settings.sendblue_allow_from
        if user_id not in allowed and "*" not in allowed:
            raise ValueError("Sendblue recipient is not in the allowlist")
        for offset in range(0, len(text), 2000):
            try:
                response = await self._client.post(
                    "/api/send-message",
                    json={
                        "number": user_id,
                        "from_number": self._settings.sendblue_from_number,
                        "content": text[offset : offset + 2000],
                    },
                )
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                raise RuntimeError("Sendblue delivery unconfirmed; inspect provider status before retrying") from exc
            if (
                not isinstance(data, dict)
                or not isinstance(data.get("message_handle"), str)
                or not data["message_handle"]
                or data.get("error_code") not in (None, 0)
                or data.get("status") not in ("QUEUED", "SENT", "DELIVERED", "READ")
            ):
                raise RuntimeError("Sendblue did not confirm acceptance; inspect provider status before retrying")

    async def send_file(self, user_id: str, file_path: str, caption: str = "") -> None:
        raise NotImplementedError("Sendblue supports text only; file delivery is unavailable")

    async def send_photo_from_bytes(
        self, user_id: str, data: bytes, mimetype: str = "image/png", caption: str = ""
    ) -> None:
        raise NotImplementedError("Sendblue supports text only; image upload is unavailable")
