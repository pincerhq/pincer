"""The HTTP API rate limits (configuration and behaviour: docs/core-components/rate-limiting-and-caching.md).

- `APIRateLimits.by_ip`: the pre-auth IP ceiling, run by `RateLimitMiddleware`.
- `enforce_rate_limits`: the app-level dependency, after routing and auth: a
  route's `@rate_limit`s, nothing for `@no_rate_limit`, else the default user
  (or, without an identity, anonymous) limit.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, Request, Response
from starlette.requests import HTTPConnection

from pincer.api.auth_guard import SELF_AUTHENTICATED_PREFIXES, client_ip
from pincer.core.ratelimit import AsyncLimiter, AsyncLimiterFactory, Charge, Rate, acharge_all
from pincer.core.ratelimit.decorators import LimitScope, RouteLimit, route_limits
from pincer.core.ratelimit.middleware import Scope, rate_limit_headers

EXEMPT_PATHS = frozenset({"/api/health"})


def is_exempt(path: str) -> bool:
    return path in EXEMPT_PATHS or not path.startswith("/api/") or path.startswith(SELF_AUTHENTICATED_PREFIXES)


#: How each built-in scope names the caller (`LimitScope.USER` needs an identity: see `_identify`).
_IDENTIFIERS: dict[LimitScope, Callable[[Request, Any], str]] = {
    LimitScope.USER: lambda request, identity: str(identity.pincer_user_id),
    LimitScope.IP: lambda request, identity: client_ip(request),
    LimitScope.ANON: lambda request, identity: client_ip(request),
    LimitScope.GLOBAL: lambda request, identity: "all",
}


def _identify(limit: RouteLimit, request: Request) -> tuple[str, str] | None:
    """`(bucket scope, identifier)` for this request, or None when the limit doesn't apply to it."""
    identity = getattr(request.state, "identity", None)
    scope = limit.scope
    if not isinstance(scope, LimitScope):
        identifier = scope(request)
        return None if identifier is None else (limit.scope_name, identifier)
    if scope is LimitScope.USER and identity is None:
        scope = LimitScope.ANON  # no identity to count: the caller's IP, like the default limit
    return scope.value, _IDENTIFIERS[scope](request, identity)


@dataclass
class APIRateLimits:
    """The default limiters and the per-route ones, on one storage (`cache_backend_url`)."""

    ip: AsyncLimiter
    user: AsyncLimiter
    anon: AsyncLimiter
    factory: AsyncLimiterFactory
    _routes: dict[tuple[str, RouteLimit], AsyncLimiter] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, settings: Any) -> APIRateLimits:
        factory = AsyncLimiterFactory.from_settings(settings)
        return cls(
            ip=factory.limiter(Rate.per_minute(settings.rate_api_ip_per_min), "ip"),
            user=factory.limiter(Rate.per_minute(settings.rate_api_user_per_min), "user"),
            anon=factory.limiter(Rate.per_minute(settings.rate_api_anon_per_min), "anon"),
            factory=factory,
        )

    def by_ip(self, scope: Scope) -> tuple[AsyncLimiter, str] | None:
        """The pre-auth ceiling: every non-exempt request, by client IP."""
        if is_exempt(scope["path"]):
            return None
        return self.ip, client_ip(HTTPConnection(scope))

    def default_charge(self, request: Request) -> Charge[AsyncLimiter]:
        """The default post-auth bucket: the caller's user, else the anonymous one by IP."""
        identity = getattr(request.state, "identity", None)
        if identity is not None:
            return Charge(self.user, identity.pincer_user_id)
        return Charge(self.anon, client_ip(request))

    def route_charge(self, route: str, limit: RouteLimit, request: Request) -> Charge[AsyncLimiter] | None:
        """What a `@rate_limit` declaration charges for this request (None: not limited)."""
        identified = _identify(limit, request)
        if identified is None:
            return None
        scope_name, identifier = identified
        rate = limit.current_rate()
        bucket = limit.bucket_name(route, rate)
        key = (f"{scope_name}:{bucket}", limit)
        limiter = self._routes.get(key)
        if limiter is None:
            limiter = self.factory.limiter(rate, scope_name, limit.strategy, route=bucket)
            self._routes[key] = limiter
        return Charge(limiter, identifier, limit.cost, limit.detail)


async def enforce_rate_limits(request: Request, response: Response) -> None:
    """App-level dependency: the route's `@rate_limit`s, none for `@no_rate_limit`, else the default.

    The limits are checked before any is charged (`acharge_all`).
    """
    limits: APIRateLimits | None = getattr(request.app.state, "rate_limits", None)
    if limits is None or is_exempt(request.url.path):
        return
    route = request.scope.get("route")
    declared = route_limits(getattr(route, "endpoint", None))
    if declared is None:
        return
    if declared:
        route_name = str(getattr(route, "name", "") or "route")
        charges = [c for c in (limits.route_charge(route_name, limit, request) for limit in declared) if c]
    else:
        charges = [limits.default_charge(request)]
    if not charges:
        return
    charge, result = await acharge_all(charges)
    headers = {name.decode(): value.decode() for name, value in rate_limit_headers(result)}
    if not result.allowed:
        headers["Retry-After"] = str(max(1, math.ceil(result.retry_after or 1)))
        raise HTTPException(status_code=429, detail=charge.message(result), headers=headers)
    if not result.failed:  # a fail-open result knows nothing about the bucket
        response.headers.update(headers)
