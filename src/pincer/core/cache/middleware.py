"""Opt-in HTTP response cache: hishel's RFC 9111 ASGI cache, only for routes that ask for it.

An endpoint opts in with the `cache_control` dependency, which sets its
`Cache-Control` header (hishel's `hishel.fastapi.cache`) and marks the route:

    @router.get("/slo", dependencies=[cache_control(max_age=60, public=True)])

`HTTPCacheMiddleware` sends only `GET`/`HEAD` requests for marked routes
through `hishel.asgi.ASGICacheMiddleware`; every other request passes straight
through. hishel reads a whole response into memory before sending it, so
routing everything through it would break server-sent events and buffer large
downloads.

The cache is shared (`CacheOptions(shared=True)`) and its key is method + URL
only: it never sees who asked. RFC 9111 keeps a shared cache from storing a
response to an authenticated request unless the response says `public` (or
`s-maxage` / `must-revalidate`), so mark only routes whose response is the same
for every caller. The middleware sits inside auth and the rate limiters, so a
cache hit still needs a valid token and still counts.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from typing import TYPE_CHECKING, Any

from hishel import AsyncBaseStorage, CacheOptions
from hishel.asgi import ASGICacheMiddleware
from hishel.fastapi import cache as _hishel_cache

if TYPE_CHECKING:
    import re

logger = logging.getLogger(__name__)

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

CACHEABLE_METHODS = frozenset({"GET", "HEAD"})
_MARKER = "__pincer_http_cache__"


def cache_control(**directives: Any) -> Any:
    """`Cache-Control` for an endpoint (hishel's directives) that also opts its route into the cache.

    Use `public=True` for a response the shared cache may store and give to
    every caller; without it, the header only reaches the client.
    """
    depends = _hishel_cache(**directives)
    setattr(depends.dependency, _MARKER, True)
    return depends


def _uses_cache_control(dependant: Any) -> bool:
    if getattr(dependant.call, _MARKER, False):
        return True
    return any(_uses_cache_control(sub) for sub in dependant.dependencies)


def cached_paths(routes: Iterable[Any]) -> list[re.Pattern[str]]:
    """Path patterns of the routes whose dependencies include `cache_control`.

    Walks included routers too (`fastapi.routing.iter_route_contexts`, which
    resolves each route's full path under its router prefixes). Routes with
    `@cache_response` are left out: the decorator caches them.
    """
    from fastapi.routing import APIRoute, iter_route_contexts
    from starlette.routing import compile_path

    from pincer.core.cache.decorators import cache_rule

    patterns = []
    for context in iter_route_contexts(list(routes)):
        route = context.original_route
        if not isinstance(route, APIRoute) or not context.path:
            continue
        # `@cache_response` caches the route itself; it overrides `cache_control` here.
        if cache_rule(route.endpoint) is None and _uses_cache_control(route.dependant):
            patterns.append(compile_path(context.path)[0])
    return patterns


class HTTPCacheMiddleware:
    """Pure ASGI: cacheable requests for opted-in routes go through hishel, the rest pass through."""

    def __init__(self, app: ASGIApp, storage: AsyncBaseStorage) -> None:
        self.app = app
        self.storage = storage
        self._cache = ASGICacheMiddleware(app, storage=storage, rfc=CacheOptions(shared=True))
        self._paths: list[re.Pattern[str]] | None = None

    def _opted_in(self, scope: Scope) -> bool:
        if self._paths is None:
            # Routes are all registered once the first request arrives; `scope["app"]` is the FastAPI app.
            self._paths = cached_paths(getattr(scope.get("app"), "routes", ()))
            logger.debug("HTTP cache covers %d route(s)", len(self._paths))
        path = scope["path"]
        return any(pattern.match(path) for pattern in self._paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] in CACHEABLE_METHODS and self._opted_in(scope):
            await self._cache(scope, receive, send)
            return
        await self.app(scope, receive, send)

    async def aclose(self) -> None:
        await self.storage.close()
