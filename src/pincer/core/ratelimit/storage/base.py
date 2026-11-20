from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pincer.core.ratelimit.strategies import Strategy
    from pincer.core.ratelimit.types import Rate, RateLimitResult


class RateLimitStorage(Protocol):
    """Applies a strategy to one bucket atomically and keeps its state."""

    async def hit(self, key: str, strategy: Strategy, rate: Rate, cost: int = 1) -> RateLimitResult: ...

    async def reset(self, key: str) -> None: ...
