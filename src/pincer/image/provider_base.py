"""Abstract base class for image providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pincer.image.types import ImageRequest, ImageResult


class BaseImageProvider(ABC):
    @abstractmethod
    async def generate(self, request: ImageRequest) -> ImageResult:
        """Generate images from a request. Raises on failure."""

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if required credentials/packages are present."""

    @abstractmethod
    def estimate_cost(self, request: ImageRequest) -> float:
        """Estimate cost in USD before generation (best-effort)."""
