"""Per-route rate limits: `@rate_limit(...)` and `@no_rate_limit` on an endpoint.

The decorators only attach settings to the endpoint function; nothing is
wrapped, so its signature and its sync/async nature stay as they are. The API
reads them after routing (`pincer.api.rate_limits.enforce_rate_limits`, from
`request.scope["route"].endpoint`) and they replace the default user/anon
limit for that route:

    @router.post("/chat")
    @rate_limit("20/minute")                          # replaces the default user limit
    @rate_limit("500/day", strategy="token_bucket")   # stacks: every limit must allow
    async def chat(...): ...

    @router.get("/status")
    @no_rate_limit                                    # no post-auth limit (the IP ceiling still applies)
    async def status(...): ...

`scope` names who is counted: `"user"` (the caller's identity, else their IP
as `anon`), `"ip"`, `"anon"`, `"global"` (one bucket for everyone), or a
function `request -> str | None` that returns the identifier itself (None:
not limited). `rate` may be a function (read per request, e.g. from settings)
and `detail` replaces the 429 message. Each declared limit gets its
own bucket (`<prefix>:kv:ratelimit:<scope>:<route>.<n>per<period>s[.<strategy>]:<id>`),
so a tight route limit doesn't spend the general budget, stacked limits don't
share state, and the reverse.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from pincer.core.ratelimit.strategies import STRATEGIES, SlidingWindowStrategy, Strategy
from pincer.core.ratelimit.types import Rate, RateLimitResult

F = TypeVar("F", bound=Callable[..., Any])

LIMITS_ATTR = "__pincer_rate_limits__"
SCOPES = frozenset({"user", "ip", "anon", "global"})
DEFAULT_DETAIL = "Rate limit exceeded"

ScopeFunc = Callable[[Any], "str | None"]
"""`request -> identifier` for a custom scope (None: the request is not limited)."""

RateSource = str | Rate | Callable[[], "str | Rate"]
"""A rate, or a function returning one, read on each request (e.g. from settings)."""

Detail = str | Callable[[RateLimitResult], str]
"""The 429 `detail`: a message, or a function of the refused result (e.g. to say how long to wait)."""


@dataclass(frozen=True)
class RouteLimit:
    """One limit declared on a route.

    Attributes:
        rate: Requests per period, or a function returning it (read on each request).
        scope: `"user"`, `"ip"`, `"anon"`, `"global"`, or a function returning the identifier.
        strategy: The algorithm.
        cost: Units one request spends.
        name: The bucket's route segment; defaults to the route's name. Routes
            sharing a `name` (and rate) share a budget.
        detail: The 429 message, or a function of the refused result.
    """

    rate: Rate | Callable[[], str | Rate]
    scope: str | ScopeFunc = "user"
    strategy: Strategy = SlidingWindowStrategy()
    cost: int = 1
    name: str | None = None
    detail: Detail = DEFAULT_DETAIL

    def current_rate(self) -> Rate:
        """The rate in force now (a function rate is called each time)."""
        return self.rate if isinstance(self.rate, Rate) else Rate.parse(self.rate())

    def message(self, result: RateLimitResult) -> str:
        """The 429 `detail` for a refusal by this limit."""
        return self.detail if isinstance(self.detail, str) else self.detail(result)

    def bucket_name(self, route: str, rate: Rate | None = None) -> str:
        """The bucket's route segment: the route (or `name`), the rate and the strategy.

        Each declared limit gets a bucket of its own, so stacked limits on one
        route (and their different strategy states) never share a key.
        """
        rate = rate or self.current_rate()
        bucket = f"{self.name or route}.{rate.limit}per{rate.period:g}s"
        if self.strategy.name != SlidingWindowStrategy.name:
            bucket += f".{self.strategy.name}"
        return bucket

    @property
    def scope_name(self) -> str:
        """The bucket scope segment: the scope itself, or the custom function's name."""
        if isinstance(self.scope, str):
            return self.scope
        return getattr(self.scope, "__name__", "custom")


def _strategy(value: str | Strategy | None) -> Strategy:
    if value is None:
        return SlidingWindowStrategy()
    if isinstance(value, Strategy):
        return value
    try:
        return STRATEGIES[value]()
    except KeyError:
        raise ValueError(f"Unknown rate limit strategy {value!r}; known: {', '.join(sorted(STRATEGIES))}") from None


def rate_limit(
    rate: RateSource,
    *,
    scope: str | ScopeFunc = "user",
    strategy: str | Strategy | None = None,
    cost: int = 1,
    name: str | None = None,
    detail: Detail = DEFAULT_DETAIL,
) -> Callable[[F], F]:
    """Limit this route to `rate` (e.g. `"20/minute"`) per `scope`; stack it for several limits.

    `rate` may be a function returning the rate, read on each request, so a
    limit can follow a setting. `detail` replaces the 429 message.
    """
    if isinstance(scope, str) and scope not in SCOPES:
        raise ValueError(f"Unknown rate limit scope {scope!r}; use one of {sorted(SCOPES)} or a function")
    if cost < 1:
        raise ValueError(f"Rate limit cost must be at least 1, got {cost}")
    parsed: Rate | Callable[[], str | Rate] = rate if callable(rate) else Rate.parse(rate)
    limit = RouteLimit(parsed, scope, _strategy(strategy), cost, name, detail)

    def decorate(func: F) -> F:
        existing = getattr(func, LIMITS_ATTR, ())
        if existing is None:
            raise TypeError(f"{func.__name__} is marked @no_rate_limit; it can't also have @rate_limit")
        # Decorators apply bottom-up; keep them in source order (top first).
        setattr(func, LIMITS_ATTR, (limit, *existing))
        return func

    return decorate


def no_rate_limit[F: Callable[..., Any]](func: F) -> F:
    """No post-auth rate limit on this route. The pre-auth IP ceiling still applies."""
    if getattr(func, LIMITS_ATTR, ()):
        raise TypeError(f"{func.__name__} has @rate_limit; it can't also be @no_rate_limit")
    setattr(func, LIMITS_ATTR, None)
    return func


def route_limits(endpoint: Any) -> tuple[RouteLimit, ...] | None:
    """The limits declared on *endpoint*: a tuple (empty = no decorator, use the defaults), or None (exempt).

    Looks through `functools.wraps` wrappers (e.g. `@cache_response`), so the
    decorator order doesn't matter.
    """
    seen: set[int] = set()
    func: Any = endpoint
    while func is not None and id(func) not in seen:
        seen.add(id(func))
        if hasattr(func, LIMITS_ATTR):
            limits = getattr(func, LIMITS_ATTR)
            return None if limits is None else tuple(limits)
        func = getattr(func, "__wrapped__", None)
    return ()
