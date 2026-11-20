"""Shared Redis clients per backend URL: sync (`redis.Redis`) and async (`redis.asyncio.Redis`).

The rate limiter, the HTTP caches and the cached outbound clients all take
their client from here, so a process opens one connection pool per URL and
mode. redis is imported lazily, so a `memory://`-only install never needs the
`redis` extra.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from redis import Redis
    from redis.asyncio import Redis as AsyncRedis

logger = logging.getLogger(__name__)

_clients: dict[str, Redis] = {}
_async_clients: dict[str, AsyncRedis] = {}


class RedisNotInstalledError(RuntimeError):
    """A `redis://` backend is configured but the `redis` package is missing."""


def _missing(exc: ImportError) -> RedisNotInstalledError:
    return RedisNotInstalledError(
        "A redis:// backend is configured but the redis package is missing: "
        "install pincer-agent[redis] (or set PINCER_CACHE_BACKEND_URL=memory://)"
    )


def get_redis(url: Any) -> Redis:
    """The shared sync client for *url* (a `RedisDsn` or string), created on first use."""
    key = str(url)
    client = _clients.get(key)
    if client is None:
        try:
            from redis import Redis
        except ImportError as exc:  # pragma: no cover - depends on the installed extras
            raise _missing(exc) from exc
        client = Redis.from_url(key)
        _clients[key] = client
    return client


def get_async_redis(url: Any) -> AsyncRedis:
    """The shared async client for *url* (a `RedisDsn` or string), created on first use."""
    key = str(url)
    client = _async_clients.get(key)
    if client is None:
        try:
            from redis.asyncio import Redis as AsyncRedis
        except ImportError as exc:  # pragma: no cover - depends on the installed extras
            raise _missing(exc) from exc
        client = AsyncRedis.from_url(key)
        _async_clients[key] = client
    return client


def close_all() -> None:
    """Close every shared sync client; call once on shutdown."""
    clients = list(_clients.values())
    _clients.clear()
    for client in clients:
        try:
            client.close()
        except Exception:  # noqa: BLE001 - shutdown must go on
            logger.warning("Closing a Redis client failed", exc_info=True)


async def aclose_all() -> None:
    """Close every shared async client; call once on shutdown."""
    clients = list(_async_clients.values())
    _async_clients.clear()
    for client in clients:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001 - shutdown must go on
            logger.warning("Closing a Redis client failed", exc_info=True)
