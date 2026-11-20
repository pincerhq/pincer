"""Shared backend plumbing: key names, backend clients and the HTTP response cache.

- `keys`: `KeySpace`, the one builder for every stored key, stream and channel name.
- `backend`: one `redis.asyncio` client per URL, for every Redis user in the process.
- `storage`: hishel storages (Redis, or in-process memory) under the configured prefix.
- `middleware`: `HTTPCacheMiddleware` and the `cache_control` endpoint opt-in.

Kept import-light on purpose: `pincer.config` imports `keys`, so nothing here
may pull in hishel, redis or the agent at package import time.
"""
