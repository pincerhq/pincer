"""Rate-limit algorithms, each in two equivalent forms: Python (memory) and Lua (Redis).

A strategy keeps a small state per bucket — a list of floats — and turns
`(state, now, rate, cost)` into `(new_state, result)`. The Python `apply` is a
pure function, so tests drive it with any clock. The Lua `script` does the same
on Redis in one atomic call, using the server's `TIME` so several app servers
agree on the clock. `KEYS[1]` is the bucket, `ARGV` = limit, period, cost,
dry (`1`: report the outcome without changing the bucket); it returns
`{allowed, remaining, reset_after_ms, retry_after_ms}` and sets a TTL on the
bucket.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import ClassVar

from pincer.core.ratelimit.types import Rate, RateLimitResult


class Strategy(ABC):
    """Base class of every rate-limit algorithm.

    A subclass sets `name` (its registry key) and `script` (the Lua twin of
    `apply`) and implements `apply`; both are checked when the class is defined.
    """

    name: ClassVar[str]
    script: ClassVar[str]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if not getattr(cls, "__abstractmethods__", None):  # concrete strategies only
            for attr in ("name", "script"):
                if not isinstance(getattr(cls, attr, None), str) or not getattr(cls, attr):
                    raise TypeError(f"{cls.__name__} must define a non-empty `{attr}`")

    @abstractmethod
    def apply(
        self, state: list[float] | None, now: float, rate: Rate, cost: int = 1
    ) -> tuple[list[float], RateLimitResult]:
        """Charge `cost` to a bucket in `state` at `now`: the new state and the outcome."""

    def ttl(self, rate: Rate) -> float:
        """Seconds a bucket must outlive its last hit (the Lua scripts use the same, `period * 2`)."""
        return rate.period * 2


class SlidingWindowStrategy(Strategy):
    """Weighted current + previous fixed window: smooth like a sliding log, O(1) memory.

    State: `[window_index, current_count, previous_count]`. The estimate is
    `previous * (1 - elapsed_fraction) + current`, so a burst at the end of one
    window still counts against the start of the next.
    """

    name: ClassVar[str] = "sliding_window"

    def apply(
        self, state: list[float] | None, now: float, rate: Rate, cost: int = 1
    ) -> tuple[list[float], RateLimitResult]:
        window = math.floor(now / rate.period)
        stored_window, current, previous = state if state else (window, 0.0, 0.0)
        if stored_window != window:
            previous = current if stored_window == window - 1 else 0.0
            current = 0.0
        window_start = window * rate.period
        elapsed = (now - window_start) / rate.period
        estimate = previous * (1 - elapsed) + current
        reset_after = window_start + rate.period - now
        if estimate + cost <= rate.limit:
            current += cost
            remaining = max(0, math.floor(rate.limit - estimate - cost))
            return [window, current, previous], RateLimitResult(True, rate.limit, remaining, reset_after)
        if current + cost > rate.limit or previous <= 0:
            # Only the next window can make room, and there this window's count is
            # `previous`: current * (1 - x) + cost <= limit  =>  x >= 1 - (limit - cost) / current
            carried = max(0.0, 1 - (rate.limit - cost) / current) if current > 0 else 0.0
            retry_after = reset_after + carried * rate.period
        else:
            # previous * (1 - x) + current + cost <= limit  =>  x >= 1 - (limit - current - cost) / previous
            needed = 1 - (rate.limit - current - cost) / previous
            retry_after = max(0.0, (needed - elapsed) * rate.period)
        return [window, current, previous], RateLimitResult(False, rate.limit, 0, reset_after, retry_after)

    script: ClassVar[str] = """
local limit = tonumber(ARGV[1])
local period = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local window = math.floor(now / period)
local state = redis.call('HMGET', KEYS[1], 'w', 'c', 'p')
local stored = tonumber(state[1]) or window
local current = tonumber(state[2]) or 0
local previous = tonumber(state[3]) or 0
if stored ~= window then
  if stored == window - 1 then previous = current else previous = 0 end
  current = 0
end
local window_start = window * period
local elapsed = (now - window_start) / period
local estimate = previous * (1 - elapsed) + current
local reset_after = window_start + period - now
local allowed = 0
local remaining = 0
local retry_after = 0
if estimate + cost <= limit then
  allowed = 1
  current = current + cost
  remaining = math.max(0, math.floor(limit - estimate - cost))
elseif current + cost > limit or previous <= 0 then
  local carried = 0
  if current > 0 then carried = math.max(0, 1 - (limit - cost) / current) end
  retry_after = reset_after + carried * period
else
  local needed = 1 - (limit - current - cost) / previous
  retry_after = math.max(0, (needed - elapsed) * period)
end
if ARGV[4] ~= '1' then
  redis.call('HSET', KEYS[1], 'w', window, 'c', current, 'p', previous)
  redis.call('PEXPIRE', KEYS[1], math.ceil(period * 2000))
end
return {allowed, remaining, math.ceil(reset_after * 1000), math.ceil(retry_after * 1000)}
"""


class TokenBucketStrategy(Strategy):
    """`limit` tokens, refilled continuously at `limit / period` per second; allows bursts up to `limit`.

    State: `[tokens, updated_at]`.
    """

    name: ClassVar[str] = "token_bucket"

    def apply(
        self, state: list[float] | None, now: float, rate: Rate, cost: int = 1
    ) -> tuple[list[float], RateLimitResult]:
        refill = rate.limit / rate.period
        tokens, updated_at = state if state else (float(rate.limit), now)
        tokens = min(float(rate.limit), tokens + max(0.0, now - updated_at) * refill)
        if tokens >= cost:
            tokens -= cost
            reset_after = (rate.limit - tokens) / refill
            return [tokens, now], RateLimitResult(True, rate.limit, math.floor(tokens), reset_after)
        reset_after = (rate.limit - tokens) / refill
        retry_after = (cost - tokens) / refill
        return [tokens, now], RateLimitResult(False, rate.limit, 0, reset_after, retry_after)

    script: ClassVar[str] = """
local limit = tonumber(ARGV[1])
local period = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local refill = limit / period
local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1]) or limit
local updated_at = tonumber(state[2]) or now
tokens = math.min(limit, tokens + math.max(0, now - updated_at) * refill)
local allowed = 0
local retry_after = 0
if tokens >= cost then
  allowed = 1
  tokens = tokens - cost
else
  retry_after = (cost - tokens) / refill
end
local reset_after = (limit - tokens) / refill
if ARGV[4] ~= '1' then
  redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now))
  redis.call('PEXPIRE', KEYS[1], math.ceil(period * 2000))
end
local remaining = 0
if allowed == 1 then remaining = math.floor(tokens) end
return {allowed, remaining, math.ceil(reset_after * 1000), math.ceil(retry_after * 1000)}
"""


STRATEGIES: dict[str, type[Strategy]] = {
    SlidingWindowStrategy.name: SlidingWindowStrategy,
    TokenBucketStrategy.name: TokenBucketStrategy,
}
