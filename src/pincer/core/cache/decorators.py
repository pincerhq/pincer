"""Per-route response caching: `@cache_response(...)` on an endpoint.

Unlike the shared hishel cache (`HTTPCacheMiddleware`), this runs inside the
route, after auth and the rate limits, so it can cache per user:

    @router.get("/costs/today")
    @cache_response(ttl=60)                       # scope="user": one entry per caller
    async def get_today_costs(...): ...

    @router.get("/integrations")
    @cache_response(ttl=300, scope="public", vary_headers=("accept-language",))
    def list_integrations(): ...                  # sync endpoint: the sync store

- **What is cached** is the endpoint's return value, as `jsonable_encoder` JSON.
  A hit is returned as the endpoint's value, so FastAPI applies the route's
  `response_model`, filtering and status code exactly as on a miss. A
  returned `Response` (file, stream, redirect) is passed through uncached.
- **`scope`** names whose entry it is: `"user"` (the caller's identity; a
  request without one is not cached), `"public"` (everyone shares it), or a
  function `request -> str | None` (None: not cached).
- **The key** is `<prefix>:kv:apicache:<route>:<owner>:<hash>`, the hash covering
  method, path, the query string (`vary_query`) and `vary_headers`. `<route>`
  is the endpoint's module-qualified name (`pincer.api.costs.get_today_costs`)
  unless `name=` sets one, so endpoints with the same function name in two
  routers never share entries.
- **Headers:** `X-Cache: HIT | MISS | BYPASS`, and `Cache-Control: private,
  max-age=<ttl>`; `public` only for `scope="public"` on a request without an
  `Authorization` header, so a proxy never shares an authenticated answer.
- **Invalidation:** `response_cache(app).invalidate(endpoint, user=...)` (or
  `ainvalidate`) from the routes that change the data; the endpoint function
  or its cache name.
- A store failure is logged once and the route is served uncached.
- A route with both `@cache_response` and `cache_control(...)` is cached by the
  decorator only: `HTTPCacheMiddleware` skips it.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from fastapi import Request, Response
from fastapi.encoders import jsonable_encoder
from starlette.responses import Response as StarletteResponse

from pincer.core.cache.keys import KeySpace, key_part
from pincer.core.cache.kv import AsyncKeyValueStore, KeyValueStore, create_kv_stores
from pincer.core.log_once import LogOnce

logger = logging.getLogger(__name__)
_STORE_FAILED = LogOnce(logger, recovery="Response cache store recovered")

F = TypeVar("F", bound=Callable[..., Any])

CACHE_ATTR = "__pincer_cache_response__"
_REQUEST_PARAM = "_pincer_cache_request"
_RESPONSE_PARAM = "_pincer_cache_response"

OwnerFunc = Callable[[Any], "str | None"]

Route = str | Callable[..., Any]
"""A cached endpoint, or its cache name (`CacheRule.name`)."""


@dataclass(frozen=True)
class CacheRule:
    """One `@cache_response` declaration."""

    name: str
    ttl: float | None
    scope: str | OwnerFunc
    vary_query: bool
    vary_headers: tuple[str, ...]

    def owner(self, request: Request) -> str | None:
        """Whose entry this request reads and writes; None: not cached."""
        if self.scope == "public":
            return "public"
        if self.scope == "user":
            identity = getattr(request.state, "identity", None)
            return None if identity is None else str(identity.pincer_user_id)
        assert callable(self.scope)
        return self.scope(request)

    def digest(self, request: Request) -> str:
        parts = [request.method, request.url.path]
        if self.vary_query:
            parts.append("&".join(f"{k}={v}" for k, v in sorted(request.query_params.multi_items())))
        parts.extend(f"{name}={request.headers.get(name, '')}" for name in self.vary_headers)
        return hashlib.sha256("\n".join(parts).encode()).hexdigest()


@dataclass
class ResponseCache:
    """The stores and names behind `@cache_response`, one per app (`app.state.response_cache`)."""

    keys: KeySpace
    store: KeyValueStore
    astore: AsyncKeyValueStore
    default_ttl: float

    @classmethod
    def from_settings(cls, settings: Any) -> ResponseCache:
        store, astore = create_kv_stores(settings.cache_backend_url)
        return cls(KeySpace.for_cache(settings), store, astore, float(settings.cache_default_ttl))

    def key(self, rule: CacheRule, owner: str, request: Request) -> str:
        return self.keys.api_cache(key_part(rule.name), owner, rule.digest(request))

    def prefix(self, route: Route, user: str | None = None) -> str:
        """Every entry of `route`, or of one owner (`user`, or `"public"`) on it."""
        name = key_part(cache_name(route))
        if user is None:
            return self.keys.kv("apicache", name) + ":"
        return self.keys.kv("apicache", name, key_part(user)) + ":"

    def invalidate(self, route: Route, *, user: str | None = None) -> int:
        """Drop the cached results of `route` (for one `user`, or all); the number dropped."""
        return self.store.delete_prefix(self.prefix(route, user))

    async def ainvalidate(self, route: Route, *, user: str | None = None) -> int:
        return await self.astore.delete_prefix(self.prefix(route, user))


def response_cache(app: Any) -> ResponseCache:
    """The app's `ResponseCache`, built from the settings on first use when `create_app` didn't."""
    cache = getattr(app.state, "response_cache", None)
    if cache is None:
        from pincer.config import get_settings_relaxed

        cache = ResponseCache.from_settings(get_settings_relaxed())
        app.state.response_cache = cache
    return cache


def _signature(func: Callable[..., Any]) -> inspect.Signature:
    """The evaluated signature, as FastAPI reads it (`fastapi.dependencies.utils._get_signature`)."""
    try:
        return inspect.signature(func, eval_str=True)
    except NameError:
        if sys.version_info >= (3, 14):
            from annotationlib import Format

            return inspect.signature(func, annotation_format=Format.FORWARDREF)
        return inspect.signature(func)


def _with_injected(signature: inspect.Signature) -> inspect.Signature:
    """*signature* plus the keyword-only `Request` and `Response` the wrapper needs."""
    extra = [
        inspect.Parameter(_REQUEST_PARAM, inspect.Parameter.KEYWORD_ONLY, annotation=Request),
        inspect.Parameter(_RESPONSE_PARAM, inspect.Parameter.KEYWORD_ONLY, annotation=Response),
    ]
    params = list(signature.parameters.values())
    tail = [p for p in params if p.kind is inspect.Parameter.VAR_KEYWORD]
    head = [p for p in params if p.kind is not inspect.Parameter.VAR_KEYWORD]
    return signature.replace(parameters=[*head, *extra, *tail])


def _headers(request: Request, response: Response, rule: CacheRule, ttl: float, state: str) -> None:
    response.headers["X-Cache"] = state
    if state != "BYPASS":
        # `scope="public"` shares the entry inside pincer. Downstream caches
        # (proxies, CDNs) only get `public` for a request without credentials:
        # an authenticated answer must never be handed to someone else by them.
        shared = rule.scope == "public" and "authorization" not in request.headers
        response.headers["Cache-Control"] = f"{'public' if shared else 'private'}, max-age={int(ttl)}"


def _encode(result: Any) -> bytes:
    return json.dumps(jsonable_encoder(result), separators=(",", ":")).encode()


def cache_response(
    *,
    ttl: float | None = None,
    scope: str | OwnerFunc = "user",
    vary_query: bool = True,
    vary_headers: tuple[str, ...] = (),
    name: str | None = None,
) -> Callable[[F], F]:
    """Cache this endpoint's return value for `ttl` seconds (default `PINCER_CACHE_DEFAULT_TTL`) per `scope`."""
    if isinstance(scope, str) and scope not in {"user", "public"}:
        raise ValueError(f"Unknown cache scope {scope!r}; use 'user', 'public' or a function")
    if ttl is not None and ttl <= 0:
        raise ValueError(f"Cache ttl must be positive, got {ttl}")

    def decorate(func: F) -> F:
        rule = CacheRule(
            name or f"{func.__module__}.{func.__qualname__}",
            ttl,
            scope,
            vary_query,
            tuple(h.lower() for h in vary_headers),
        )
        signature = _with_injected(_signature(func))

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                request: Request = kwargs.pop(_REQUEST_PARAM)
                response: Response = kwargs.pop(_RESPONSE_PARAM)
                cache = response_cache(request.app)
                lifetime = rule.ttl or cache.default_ttl
                owner = rule.owner(request)
                if owner is None:
                    _headers(request, response, rule, lifetime, "BYPASS")
                    return await func(*args, **kwargs)
                key = cache.key(rule, owner, request)
                try:
                    hit = await cache.astore.get(key)
                except Exception:  # noqa: BLE001 - a broken store must not break the route
                    _STORE_FAILED.log("Response cache store failed; serving %s uncached", rule.name, exc_info=True)
                    hit = None
                if hit is not None:
                    _STORE_FAILED.clear()
                    _headers(request, response, rule, lifetime, "HIT")
                    return json.loads(hit)
                result = await func(*args, **kwargs)
                if isinstance(result, StarletteResponse):
                    return result
                try:
                    await cache.astore.set(key, _encode(result), lifetime)
                    _STORE_FAILED.clear()
                except Exception:  # noqa: BLE001
                    _STORE_FAILED.log("Response cache store failed; serving %s uncached", rule.name, exc_info=True)
                _headers(request, response, rule, lifetime, "MISS")
                return result

            wrapper: Any = async_wrapper
        else:

            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                request: Request = kwargs.pop(_REQUEST_PARAM)
                response: Response = kwargs.pop(_RESPONSE_PARAM)
                cache = response_cache(request.app)
                lifetime = rule.ttl or cache.default_ttl
                owner = rule.owner(request)
                if owner is None:
                    _headers(request, response, rule, lifetime, "BYPASS")
                    return func(*args, **kwargs)
                key = cache.key(rule, owner, request)
                try:
                    hit = cache.store.get(key)
                except Exception:  # noqa: BLE001 - a broken store must not break the route
                    _STORE_FAILED.log("Response cache store failed; serving %s uncached", rule.name, exc_info=True)
                    hit = None
                if hit is not None:
                    _STORE_FAILED.clear()
                    _headers(request, response, rule, lifetime, "HIT")
                    return json.loads(hit)
                result = func(*args, **kwargs)
                if isinstance(result, StarletteResponse):
                    return result
                try:
                    cache.store.set(key, _encode(result), lifetime)
                    _STORE_FAILED.clear()
                except Exception:  # noqa: BLE001
                    _STORE_FAILED.log("Response cache store failed; serving %s uncached", rule.name, exc_info=True)
                _headers(request, response, rule, lifetime, "MISS")
                return result

            wrapper = sync_wrapper

        wrapper.__signature__ = signature
        setattr(wrapper, CACHE_ATTR, rule)
        return wrapper  # type: ignore[no-any-return]

    return decorate


def cache_rule(endpoint: Any) -> CacheRule | None:
    """The `@cache_response` rule on *endpoint* (the outermost one), if any."""
    return getattr(endpoint, CACHE_ATTR, None)


def cache_name(route: Route) -> str:
    """The cache name of a `@cache_response` endpoint, or *route* itself when it is already a name."""
    if isinstance(route, str):
        return route
    rule = cache_rule(route)
    if rule is None:
        raise ValueError(f"{getattr(route, '__qualname__', route)!r} has no @cache_response")
    return rule.name
