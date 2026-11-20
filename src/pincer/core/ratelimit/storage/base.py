from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pincer.core.ratelimit.strategies import Strategy
    from pincer.core.ratelimit.types import Rate, RateLimitResult


class RateLimitStorage(ABC):
    """Base class of the bucket stores: applies a strategy to one bucket atomically and keeps its state."""

    @abstractmethod
    async def hit(self, key: str, strategy: Strategy, rate: Rate, cost: int = 1) -> RateLimitResult:
        """Charge `cost` to the bucket `key` and return the outcome."""

    @abstractmethod
    async def reset(self, key: str) -> None:
        """Forget the bucket `key`."""
