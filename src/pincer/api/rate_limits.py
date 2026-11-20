"""The HTTP API rate limits, around `auth_middleware` in `pincer.api.server`.

    CORS → IP ceiling → auth_middleware → HTTP cache → routing → enforce_rate_limits → endpoint

- **IP ceiling** (pre-auth middleware, `PINCER_RATE_API_IP_PER_MIN`, default
  3000): every request, keyed by client IP. A flood backstop that keeps a
  single host away from the auth lookups and from reading request bodies. It is
  deliberately high: a whole office behind one NAT address shares it. Route
  decorators never change it.
- **`enforce_rate_limits`** (an app-level dependency, so after routing and auth):
  - a route with `@rate_limit(...)` gets only its own limits, each in a bucket
    of its own (`<prefix>:kv:ratelimit:<scope>:<route>.<n>per<period>s:<id>`);
  - a route with `@no_rate_limit` gets none;
  - any other route gets the default: the **user limit**
    (`PINCER_RATE_API_USER_PER_MIN`, keyed by `pincer_user_id`, so colleagues
    behind one IP each have their own budget), or for a request without an
    identity the **anonymous limit** (`PINCER_RATE_API_ANON_PER_MIN`, by IP).

Only FastAPI routes (`APIRoute`) run app-level dependencies: the built-in
`/api/docs` and `/openapi.json` pages have the IP ceiling only.

A request with a bad token is answered 401 by auth and never charged to a user;
`AuthGuard` locks out an IP that keeps guessing. No limit applies to
`/api/health`, non-`/api/` paths (dashboard assets) or the self-authenticated
webhook prefixes, whose callers (Twilio, Teams) share platform IPs.

FastAPI reads a request's body before it solves dependencies, so a request the
dependency refuses has had its body read; the IP ceiling still refuses floods
before that.

`client_ip` trusts the first `X-Forwarded-For` entry without checking it, so
the IP-keyed limits only hold behind a proxy that overwrites that header.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, Request, Response
from starlette.requests import HTTPConnection

from pincer.api.auth_guard import SELF_AUTHENTICATED_PREFIXES, client_ip
from pincer.core.cache.keys import KeySpace
from pincer.core.ratelimit import AsyncLimiter, AsyncRateLimitStorage, Rate, RateLimitResult, create_async_storage
from pincer.core.ratelimit.decorators import RouteLimit, route_limits
from pincer.core.ratelimit.middleware import Scope, rate_limit_headers

EXEMPT_PATHS = frozenset({"/api/health"})


def is_exempt(path: str) -> bool:
    return path in EXEMPT_PATHS or not path.startswith("/api/") or path.startswith(SELF_AUTHENTICATED_PREFIXES)


@dataclass
class APIRateLimits:
    """The default limiters and the per-route ones, sharing one storage on `cache_backend_url`."""

    ip: AsyncLimiter
    user: AsyncLimiter
    anon: AsyncLimiter
    storage: AsyncRateLimitStorage
    keys: KeySpace
    _routes: dict[tuple[str, RouteLimit], AsyncLimiter] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, settings: Any) -> APIRateLimits:
        storage = create_async_storage(settings.cache_backend_url)
        keys = KeySpace.for_cache(settings)
        return cls(
            ip=AsyncLimiter(storage, Rate.per_minute(settings.rate_api_ip_per_min), keys, "ip"),
            user=AsyncLimiter(storage, Rate.per_minute(settings.rate_api_user_per_min), keys, "user"),
            anon=AsyncLimiter(storage, Rate.per_minute(settings.rate_api_anon_per_min), keys, "anon"),
            storage=storage,
            keys=keys,
        )

    def by_ip(self, scope: Scope) -> tuple[AsyncLimiter, str] | None:
        """The pre-auth ceiling: every non-exempt request, by client IP."""
        if is_exempt(scope["path"]):
            return None
        return self.ip, client_ip(HTTPConnection(scope))

    def default_for(self, request: Request) -> tuple[AsyncLimiter, str]:
        """The default post-auth bucket: the caller's user, else the anonymous one by IP."""
        identity = getattr(request.state, "identity", None)
        if identity is not None:
            return self.user, identity.pincer_user_id
        return self.anon, client_ip(request)

    def for_route(self, route: str, limit: RouteLimit, request: Request) -> tuple[AsyncLimiter, str] | None:
        """The bucket a `@rate_limit` declaration charges for this request (None: not limited)."""
        scope = limit.scope
        identity = getattr(request.state, "identity", None)
        if scope == "user" and identity is None:
            scope = "anon"  # no identity to count: the caller's IP, like the default limit
        if scope == "user" and identity is not None:
            identifier: str | None = identity.pincer_user_id
        elif scope in ("ip", "anon"):
            identifier = client_ip(request)
        else:
            assert callable(scope)
            identifier = scope(request)
        if identifier is None:
            return None
        scope_name = scope if isinstance(scope, str) else limit.scope_name
        bucket = limit.bucket_name(route)
        cache_key = (f"{scope_name}:{bucket}", limit)
        limiter = self._routes.get(cache_key)
        if limiter is None:
            limiter = AsyncLimiter(self.storage, limit.rate, self.keys, scope_name, limit.strategy, route=bucket)
            self._routes[cache_key] = limiter
        return limiter, identifier


def _tightest(results: list[RateLimitResult]) -> RateLimitResult:
    """The result to report: a refusal with the longest wait, else the one with the least room left."""
    denied = [r for r in results if not r.allowed]
    if denied:
        return max(denied, key=lambda r: r.retry_after)
    return min(results, key=lambda r: (r.remaining / r.limit, r.remaining))


async def enforce_rate_limits(request: Request, response: Response) -> None:
    """App-level dependency: the route's `@rate_limit`s, none for `@no_rate_limit`, else the default."""
    limits: APIRateLimits | None = getattr(request.app.state, "rate_limits", None)
    if limits is None or is_exempt(request.url.path):
        return
    route = request.scope.get("route")
    declared = route_limits(getattr(route, "endpoint", None))
    if declared is None:
        return
    if declared:
        route_name = str(getattr(route, "name", "") or "route")
        buckets = [limits.for_route(route_name, limit, request) for limit in declared]
        charges = [(bucket, limit.cost) for bucket, limit in zip(buckets, declared, strict=True) if bucket is not None]
    else:
        charges = [(limits.default_for(request), 1)]
    if not charges:
        return
    results = [await limiter.hit(identifier, cost) for (limiter, identifier), cost in charges]
    reported = _tightest(results)
    headers = {name.decode(): value.decode() for name, value in rate_limit_headers(reported)}
    if not reported.allowed:
        headers["Retry-After"] = str(max(1, math.ceil(reported.retry_after or 1)))
        raise HTTPException(status_code=429, detail="Rate limit exceeded", headers=headers)
    if reported.reset_after > 0 or reported.remaining != reported.limit:  # skip fail-open results
        response.headers.update(headers)
