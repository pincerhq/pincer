"""fal.ai image provider — primary provider using Nano Banana model."""

from __future__ import annotations

import logging

from pincer.image.provider_base import BaseImageProvider
from pincer.image.types import GeneratedImage, ImageRequest, ImageResult

logger = logging.getLogger(__name__)

# Cost per image for nano-banana-2 (approximately $0.003/image as of 2026)
_COST_PER_IMAGE = 0.003


class FalImageProvider(BaseImageProvider):
    """Uses fal-client to call fal-ai/nano-banana-2 (or configured model)."""

    def __init__(self, api_key: str, model: str = "fal-ai/nano-banana-2") -> None:
        self._api_key = api_key
        self._model = model

    def is_available(self) -> bool:
        if not self._api_key:
            return False
        try:
            import fal_client  # noqa: F401
            return True
        except ImportError:
            return False

    def estimate_cost(self, request: ImageRequest) -> float:
        return _COST_PER_IMAGE * request.num_images

    async def generate(self, request: ImageRequest) -> ImageResult:
        import os

        import fal_client

        # fal_client reads FAL_KEY from environment
        os.environ.setdefault("FAL_KEY", self._api_key)

        model = request.model or self._model

        # Aspect ratio -> width/height map
        _aspect_map = {
            "1:1": {"image_size": "square"},
            "16:9": {"image_size": "landscape_16_9"},
            "9:16": {"image_size": "portrait_16_9"},
            "4:3": {"image_size": "landscape_4_3"},
            "3:4": {"image_size": "portrait_4_3"},
        }
        size_arg = _aspect_map.get(request.aspect_ratio, {"image_size": "square"})

        arguments = {
            "prompt": request.prompt,
            "num_images": request.num_images,
            **size_arg,
        }

        logger.debug("fal generate: model=%s prompt=%r", model, request.prompt[:80])
        result = await fal_client.run_async(model, arguments=arguments)

        images: list[GeneratedImage] = []
        for item in result.get("images", []):
            url = item.get("url") or item.get("image", {}).get("url")
            if url:
                images.append(GeneratedImage(url=url, content_type="image/jpeg"))

        if not images:
            raise RuntimeError(f"fal returned no images: {result}")

        return ImageResult(
            images=images,
            provider="fal",
            model=model,
            cost_usd=_COST_PER_IMAGE * len(images),
        )
