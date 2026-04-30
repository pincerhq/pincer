"""Gemini image provider — fallback using google-genai with IMAGE modality."""

from __future__ import annotations

import logging

from pincer.image.provider_base import BaseImageProvider
from pincer.image.types import GeneratedImage, ImageRequest, ImageResult

logger = logging.getLogger(__name__)

# Gemini 2.5 Flash image generation cost (approx per image, 2026)
_COST_PER_IMAGE = 0.004


class GeminiImageProvider(BaseImageProvider):
    def __init__(self, api_key: str, model: str = "gemini-2.5-flash-image") -> None:
        self._api_key = api_key
        self._model = model

    def is_available(self) -> bool:
        if not self._api_key:
            return False
        try:
            from google import genai  # noqa: F401

            return True
        except ImportError:
            return False

    def estimate_cost(self, request: ImageRequest) -> float:
        return _COST_PER_IMAGE * request.num_images

    async def generate(self, request: ImageRequest) -> ImageResult:
        from google import genai
        from google.genai.types import GenerateContentConfig, Modality

        client = genai.Client(api_key=self._api_key)
        model = request.model or self._model

        # Gemini doesn't natively support num_images > 1 per call; we loop
        images: list[GeneratedImage] = []
        for _ in range(request.num_images):
            response = await client.aio.models.generate_content(
                model=model,
                contents=request.prompt,
                config=GenerateContentConfig(
                    response_modalities=[Modality.TEXT, Modality.IMAGE],
                ),
            )

            candidates = getattr(response, "candidates", []) or []
            if not candidates:
                raise RuntimeError("Gemini returned no candidates")

            content = getattr(candidates[0], "content", None)
            parts = getattr(content, "parts", []) or []
            found = False
            for part in parts:
                inline = getattr(part, "inline_data", None)
                if inline and hasattr(inline, "data") and inline.data:
                    data = inline.data if isinstance(inline.data, bytes) else bytes(inline.data)
                    mime = getattr(inline, "mime_type", None) or "image/png"
                    images.append(GeneratedImage(bytes=data, content_type=mime))
                    found = True
                    break
            if not found:
                raise RuntimeError("Gemini did not return an image in response")

        return ImageResult(
            images=images,
            provider="gemini",
            model=model,
            cost_usd=_COST_PER_IMAGE * len(images),
        )
