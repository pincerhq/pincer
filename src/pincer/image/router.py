"""Image provider router with automatic fallback (fal → gemini)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pincer.image.provider_base import BaseImageProvider
    from pincer.image.types import ImageRequest, ImageResult

logger = logging.getLogger(__name__)


class ImageProviderRouter:
    """Routes image generation to the first available provider, with fallback."""

    def __init__(self, providers: list[BaseImageProvider]) -> None:
        self._providers = providers

    async def generate(self, request: ImageRequest) -> ImageResult:
        last_error: Exception | None = None
        for provider in self._providers:
            if not provider.is_available():
                logger.debug("Skipping unavailable provider: %s", type(provider).__name__)
                continue
            try:
                result = await provider.generate(request)
                return result
            except Exception as e:
                logger.warning(
                    "Provider %s failed: %s — trying next",
                    type(provider).__name__,
                    e,
                )
                last_error = e

        raise RuntimeError(f"All image providers failed. Last error: {last_error}") from last_error


def build_router_from_settings() -> ImageProviderRouter:
    """Construct an ImageProviderRouter from current settings."""
    from pincer.config import get_settings
    from pincer.image.provider_fal import FalImageProvider
    from pincer.image.provider_gemini import GeminiImageProvider

    s = get_settings()
    providers: list[BaseImageProvider] = []

    fal_key = s.fal_key.get_secret_value() if hasattr(s, "fal_key") else ""
    gemini_key = s.gemini_api_key.get_secret_value()
    image_provider = getattr(s, "image_provider", "auto")

    if image_provider in ("fal", "auto") and fal_key:
        providers.append(FalImageProvider(api_key=fal_key, model=getattr(s, "fal_model", "fal-ai/nano-banana-2")))

    if image_provider in ("gemini", "auto") and gemini_key:
        gemini_model = getattr(s, "image_model_gemini", "gemini-2.5-flash-image")
        providers.append(GeminiImageProvider(api_key=gemini_key, model=gemini_model))

    return ImageProviderRouter(providers)
