"""The two HTTP API rate limits, wired around `auth_middleware` in `pincer.api.server`.

    CORS → IP ceiling → auth_middleware → user | anon limit → HTTP cache → routes

- **IP ceiling** (before auth, `PINCER_RATE_API_IP_PER_MIN`, default 3000): every
  request, keyed by client IP. A flood backstop that keeps a single host away
  from the auth lookups. It is deliberately high: a whole office behind one NAT
  address shares it.
- **User limit** (after auth, `PINCER_RATE_API_USER_PER_MIN`): requests with an
  identity, keyed by `pincer_user_id`, so colleagues behind one IP each have
  their own budget.
- **Anonymous limit** (after auth, `PINCER_RATE_API_ANON_PER_MIN`): requests that
  reached the routes without an identity (public paths such as login or the
  demo call, or `PINCER_AUTH_DISABLED`), keyed by client IP.

A request with a bad token is answered 401 by auth and never charged to a user;
`AuthGuard` locks out an IP that keeps guessing. Both limits skip `/api/health`,
non-`/api/` paths (dashboard assets) and the self-authenticated webhook
prefixes, whose callers (Twilio, Teams) share platform IPs.

`client_ip` trusts the first `X-Forwarded-For` entry without checking it, so
the IP-keyed limits only hold behind a proxy that overwrites that header.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from starlette.requests import HTTPConnection

from pincer.api.auth_guard import SELF_AUTHENTICATED_PREFIXES, client_ip
from pincer.core.cache.keys import KeySpace
from pincer.core.ratelimit import AsyncLimiter, Rate, create_async_storage
from pincer.core.ratelimit.middleware import Scope

EXEMPT_PATHS = frozenset({"/api/health"})


def is_exempt(path: str) -> bool:
    return path in EXEMPT_PATHS or not path.startswith("/api/") or path.startswith(SELF_AUTHENTICATED_PREFIXES)


@dataclass(frozen=True)
class APIRateLimits:
    """The three limiters, sharing one storage on `cache_backend_url`."""

    ip: AsyncLimiter
    user: AsyncLimiter
    anon: AsyncLimiter

    @classmethod
    def from_settings(cls, settings: Any) -> APIRateLimits:
        storage = create_async_storage(settings.cache_backend_url)
        keys = KeySpace.for_cache(settings)
        return cls(
            ip=AsyncLimiter(storage, Rate.per_minute(settings.rate_api_ip_per_min), keys, "ip"),
            user=AsyncLimiter(storage, Rate.per_minute(settings.rate_api_user_per_min), keys, "user"),
            anon=AsyncLimiter(storage, Rate.per_minute(settings.rate_api_anon_per_min), keys, "anon"),
        )

    def by_ip(self, scope: Scope) -> tuple[AsyncLimiter, str] | None:
        """The pre-auth ceiling: every non-exempt request, by client IP."""
        if is_exempt(scope["path"]):
            return None
        return self.ip, client_ip(HTTPConnection(scope))

    def by_identity(self, scope: Scope) -> tuple[AsyncLimiter, str] | None:
        """After auth: the user's bucket when `auth_middleware` found one, else the anonymous one by IP."""
        if is_exempt(scope["path"]):
            return None
        identity = scope.get("state", {}).get("identity")
        if identity is not None:
            return self.user, identity.pincer_user_id
        return self.anon, client_ip(HTTPConnection(scope))
