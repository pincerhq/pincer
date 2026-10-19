"""Direct iMessage/SMS via Sendblue, with an authenticated, bounded webhook inbox."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from collections import OrderedDict
from functools import partial
from typing import TYPE_CHECKING, Any

import httpx
from starlette.responses import Response

from pincer.channels.base import BaseChannel, ChannelType, IncomingMessage
from pincer.config.channels import is_sendblue_phone

if TYPE_CHECKING:
    from starlette.requests import Request

    from pincer.channels.base import MessageHandler
    from pincer.config import Settings
    from pincer.core.identity import IdentityResolver

logger = logging.getLogger(__name__)
_MEDIA_NOTICE = "[Attachment received; this channel supports text only. Please resend its contents as text.]"


class SendblueConfigurationError(ValueError):
    """Operator-actionable configuration error containing no supplied values."""


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
        self._accepting = False
        self._tasks: set[asyncio.Task[None]] = set()
        self._senders: dict[str, asyncio.Task[None]] = {}
        self._slots = asyncio.Semaphore(8)
        self._worker: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[IncomingMessage] = asyncio.Queue(maxsize=128)
        self._seen: OrderedDict[str, None] = OrderedDict()

    @property
    def name(self) -> str:
        return "sendblue"

    @property
    def running(self) -> bool:
        return self._accepting

    async def start(self, handler: MessageHandler) -> None:
        cfg = self._settings
        if not cfg.sendblue_api_key.get_secret_value() or not cfg.sendblue_api_secret.get_secret_value():
            raise SendblueConfigurationError("Sendblue requires API key and API secret")
        if not cfg.sendblue_signing_secret.get_secret_value():
            raise SendblueConfigurationError("Sendblue requires a webhook signing secret")
        if not is_sendblue_phone(cfg.sendblue_from_number):
            raise SendblueConfigurationError("Sendblue sending line must be an E.164 number")
        if any(number != "*" and not is_sendblue_phone(number) for number in cfg.sendblue_allow_from):
            raise SendblueConfigurationError("Sendblue allowlist entries must be E.164 numbers or explicit '*'")
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
        self._worker = asyncio.create_task(self._process())
        self._accepting = True

    async def stop(self) -> None:
        # Never run queued turns against services that are shutting down.
        # Acknowledged in-memory work is not durable; cancellation is not replay.
        self._accepting = False
        if self._worker:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._senders.clear()
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
        if self._client:
            await self._client.aclose()
            self._client = None

    async def receive(self, request: Request) -> Response:
        if not self._accepting:
            return Response(status_code=503)
        supplied = request.headers.get("sb-signing-secret", "").encode("latin-1")
        expected = self._settings.sendblue_signing_secret.get_secret_value().encode()
        if not expected or not hmac.compare_digest(supplied, expected):
            return Response(status_code=401)
        try:
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > 65536:
                    return Response(status_code=413)
                body.extend(chunk)
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return Response(status_code=400)
        if not isinstance(payload, dict):
            return Response(status_code=400)
        if (
            payload.get("is_outbound") is not False
            or payload.get("status") != "RECEIVED"
            or payload.get("group_id") not in (None, "")
            or payload.get("to_number") != self._settings.sendblue_from_number
        ):
            return Response(status_code=200)
        sender, handle = payload.get("from_number"), payload.get("message_handle")
        content, media = payload.get("content"), payload.get("media_url")
        allowed = self._settings.sendblue_allow_from
        if not isinstance(sender, str) or not is_sendblue_phone(sender):
            return Response(status_code=200)
        if sender not in allowed and "*" not in allowed:
            return Response(status_code=200)
        if (
            not isinstance(handle, str)
            or not 0 < len(handle) <= 256
            or (content is not None and not isinstance(content, str))
            or (media is not None and not isinstance(media, str))
        ):
            return Response(status_code=400)
        if self._identity and self._identity.has_config and await self._identity.is_guest(self.channel_type, sender):
            return Response(status_code=200)
        if handle in self._seen:
            return Response(status_code=200)
        text = content or ""
        if media:
            text = f"{text}\n{_MEDIA_NOTICE}".strip()
        if not text.strip():
            return Response(status_code=200)
        # Body/identity awaits may race with stop(); admission below is atomic.
        if not self._accepting:
            return Response(status_code=503)
        if self._queue.qsize() + len(self._tasks) >= 128:
            return Response(status_code=503)
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
            return Response(status_code=503)
        self._seen[handle] = None
        if len(self._seen) > 4096:
            self._seen.popitem(last=False)
        return Response(status_code=200)

    async def _process(self) -> None:
        while True:
            message = await self._queue.get()
            previous = self._senders.get(message.user_id)
            task = asyncio.create_task(self._process_message(message, previous))
            self._tasks.add(task)
            self._senders[message.user_id] = task
            task.add_done_callback(partial(self._finished, message.user_id))

    def _finished(self, sender: str, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if self._senders.get(sender) is task:
            del self._senders[sender]
        # A task cancelled before its first poll never executes a finally block.
        self._queue.task_done()

    async def _process_message(self, message: IncomingMessage, previous: asyncio.Task[None] | None) -> None:
        if previous is not None:
            await asyncio.shield(previous)
        async with self._slots:
            try:
                if self._handler is None:
                    raise RuntimeError("Sendblue handler not initialized")
                reply = await self._handler(message)
            except Exception as exc:
                self._log_failure("handler", message, exc)
                # Handler failure happened before any reply POST. One best-effort
                # notice is safe; a failed or ambiguous reply is never retried.
                try:
                    await self.send(message.user_id, "Sorry, I couldn't process your message. Please try again later.")
                except Exception as notice_error:
                    self._log_failure("failure notice", message, notice_error)
                return
            if reply:
                try:
                    await self.send(message.user_id, reply)
                except Exception as exc:
                    self._log_failure("reply", message, exc)

    @staticmethod
    def _log_failure(stage: str, message: IncomingMessage, exc: Exception) -> None:
        logger.error(
            "Sendblue %s failed (%s) for message %r; no automatic replay",
            stage,
            type(exc).__name__,
            message.reply_to_message_id,
        )

    async def send(self, user_id: str, text: str, **kwargs: Any) -> None:
        if not self._client:
            raise RuntimeError("Sendblue channel is not started")
        # IdentityResolver stores phone-channel IDs without '+'. Restore E.164
        # at the provider boundary, before applying the configured allowlist.
        user_id = user_id if user_id.startswith("+") else f"+{user_id}"
        if not is_sendblue_phone(user_id):
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
                or not 0 < len(data["message_handle"]) <= 256
                or any(ord(c) < 32 for c in data["message_handle"])
                or data.get("error_code") not in (None, 0)
                or data.get("status") in ("ERROR", "DECLINED")
            ):
                raise RuntimeError("Sendblue did not confirm acceptance; inspect provider status before retrying")

    async def send_file(self, user_id: str, file_path: str, caption: str = "") -> None:
        raise NotImplementedError("Sendblue supports text only; file delivery is unavailable")

    async def send_photo_from_bytes(
        self, user_id: str, data: bytes, mimetype: str = "image/png", caption: str = ""
    ) -> None:
        raise NotImplementedError("Sendblue supports text only; image upload is unavailable")
