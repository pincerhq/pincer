"""One `redis.asyncio` client per backend URL, shared by the rate limiter and the HTTP cache.

redis is imported lazily, so a `memory://`-only install never needs the
`redis` extra.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

_clients: dict[str, Redis] = {}


class RedisNotInstalledError(RuntimeError):
    """A `redis://` backend is configured but the `redis` package is missing."""


def get_redis(url: Any) -> Redis:
    """The shared client for *url* (a `RedisDsn` or string), created on first use."""
    key = str(url)
    client = _clients.get(key)
    if client is None:
        try:
            from redis.asyncio import Redis
        except ImportError as exc:  # pragma: no cover - depends on the installed extras
            raise RedisNotInstalledError(
                "A redis:// backend is configured but the redis package is missing: "
                "install pincer-agent[redis] (or set PINCER_CACHE_BACKEND_URL=memory://)"
            ) from exc
        client = Redis.from_url(key)
        _clients[key] = client
    return client


async def aclose_all() -> None:
    """Close every shared client; call once on shutdown."""
    clients = list(_clients.values())
    _clients.clear()
    for client in clients:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001 - shutdown must go on
            logger.warning("Closing a Redis client failed", exc_info=True)
