from __future__ import annotations

import re
from dataclasses import dataclass

_UNITS = {"second": 1.0, "minute": 60.0, "hour": 3600.0, "day": 86400.0}
_RATE = re.compile(r"^\s*(\d+)\s*/\s*(?:(second|minute|hour|day)s?|(\d+(?:\.\d+)?)\s*s)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class Rate:
    """`limit` requests per `period` seconds."""

    limit: int
    period: float = 60.0

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError(f"Rate limit must be at least 1, got {self.limit}")
        if self.period <= 0:
            raise ValueError(f"Rate period must be positive, got {self.period}")

    @classmethod
    def per_minute(cls, limit: int) -> Rate:
        return cls(limit, 60.0)

    @classmethod
    def parse(cls, value: str | Rate) -> Rate:
        """`"20/minute"`, `"500/day"`, `"10/second"`, `"3/90s"` (or a `Rate`, returned as is)."""
        if isinstance(value, Rate):
            return value
        match = _RATE.match(value)
        if match is None:
            raise ValueError(f"Invalid rate {value!r}: expected '<n>/<second|minute|hour|day>' or '<n>/<seconds>s'")
        limit, unit, seconds = match.groups()
        return cls(int(limit), _UNITS[unit.lower()] if unit else float(seconds))


@dataclass(frozen=True)
class RateLimitResult:
    """The outcome of one check.

    Attributes:
        allowed: Whether the request may proceed.
        limit: The configured limit.
        remaining: Requests still allowed right now (0 when denied).
        reset_after: Seconds until the limit is fully available again.
        retry_after: Seconds until a denied request would be allowed (0 when allowed).
    """

    allowed: bool
    limit: int
    remaining: int
    reset_after: float
    retry_after: float = 0.0

    @classmethod
    def unlimited(cls, limit: int) -> RateLimitResult:
        """The fail-open answer: allowed, with no information about the bucket."""
        return cls(allowed=True, limit=limit, remaining=limit, reset_after=0.0)
