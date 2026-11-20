"""Pure-ASGI rate-limit middleware.

Pure ASGI rather than `BaseHTTPMiddleware`, so streamed responses (SSE chat)
pass through untouched. Only `http` requests are limited; websockets and
lifespan pass.
"""

from __future__ import annotations

import json
import math
from collections.abc import Awaitable, Callable, MutableMapping
from typing import TYPE_CHECKING, Any

from pincer.core.ratelimit.limiter import AsyncLimiter

if TYPE_CHECKING:
    from pincer.core.ratelimit.types import RateLimitResult

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

Identify = Callable[[Scope], tuple[AsyncLimiter, str] | None]
"""Picks the limiter and bucket for a request: `(limiter, identifier)`, or None to let it pass."""


def _seconds(value: float) -> str:
    return str(max(0, math.ceil(value)))


def rate_limit_headers(result: RateLimitResult) -> list[tuple[bytes, bytes]]:
    return [
        (b"x-ratelimit-limit", str(result.limit).encode()),
        (b"x-ratelimit-remaining", str(result.remaining).encode()),
        (b"x-ratelimit-reset", _seconds(result.reset_after).encode()),
    ]


class RateLimitMiddleware:
    """Charges each request to the bucket `identify` picks; answers 429 once it is empty.

    Args:
        app: The wrapped ASGI app.
        identify: `scope -> (limiter, identifier) | None`. None skips the
            request (exempt path, or nothing to key it by).
        expose_headers: Add `X-RateLimit-*` to allowed responses. Only one
            layer should, so a response does not carry two sets.
    """

    def __init__(
        self,
        app: ASGIApp,
        identify: Identify,
        *,
        expose_headers: bool = True,
    ) -> None:
        self.app = app
        self.identify = identify
        self.expose_headers = expose_headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        picked = self.identify(scope)
        if picked is None:
            await self.app(scope, receive, send)
            return
        limiter, identifier = picked
        result = await limiter.hit(identifier)
        if not result.allowed:
            await self._reject(result, send)
            return
        if not self.expose_headers or result.failed:
            # Nothing to report (or a fail-open result that knows nothing about the bucket).
            await self.app(scope, receive, send)
            return

        headers = rate_limit_headers(result)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                names = {name.lower() for name, _ in message.get("headers", [])}
                extra = [(name, value) for name, value in headers if name not in names]
                message["headers"] = [*message.get("headers", []), *extra]
            await send(message)

        await self.app(scope, receive, send_with_headers)

    @staticmethod
    async def _reject(result: RateLimitResult, send: Send) -> None:
        body = json.dumps({"detail": "Rate limit exceeded"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"retry-after", _seconds(result.retry_after or 1).encode()),
                    *rate_limit_headers(result),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})
