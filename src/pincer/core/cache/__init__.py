"""Shared backend plumbing: key names, backend clients and the HTTP response cache.

- `keys`: `KeySpace`, the one builder for every stored key, stream and channel name.
- `backend`: one Redis client per URL and mode (`get_redis` sync, `get_async_redis` async).
- `storage`: hishel storages under the configured prefix, sync (`MemoryStorage`,
  hishel `RedisStorage`) and async (`AsyncMemoryStorage`, hishel `AsyncRedisStorage`).
- `middleware`: `HTTPCacheMiddleware` (ASGI) and the `cache_control` endpoint opt-in.
- `client`: cached outbound httpx clients, `cached_client` (sync) and `async_cached_client`.
- `kv`: a small TTL key-value store, sync and async, in memory or Redis.
- `decorators`: `@cache_response(...)`, per-route (and per-user) caching of endpoint results.

Kept import-light on purpose: `pincer.config` imports `keys`, so nothing here
may pull in hishel, redis or the agent at package import time.
"""
