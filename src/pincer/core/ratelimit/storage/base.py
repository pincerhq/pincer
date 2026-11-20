"""The two storage contracts: `RateLimitStorage` (sync) and `AsyncRateLimitStorage` (async).

Same operations, same bucket names, same state: a sync and an async limiter on
one backend share their buckets.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pincer.core.ratelimit.strategies import Strategy
    from pincer.core.ratelimit.types import Rate, RateLimitResult


class RateLimitStorage(ABC):
    """Base class of the sync bucket stores: applies a strategy to one bucket atomically and keeps its state."""

    @abstractmethod
    def hit(self, key: str, strategy: Strategy, rate: Rate, cost: int = 1) -> RateLimitResult:
        """Charge `cost` to the bucket `key` and return the outcome."""

    @abstractmethod
    def reset(self, key: str) -> None:
        """Forget the bucket `key`."""


class AsyncRateLimitStorage(ABC):
    """Base class of the async bucket stores; the async twin of `RateLimitStorage`."""

    @abstractmethod
    async def hit(self, key: str, strategy: Strategy, rate: Rate, cost: int = 1) -> RateLimitResult:
        """Charge `cost` to the bucket `key` and return the outcome."""

    @abstractmethod
    async def reset(self, key: str) -> None:
        """Forget the bucket `key`."""
