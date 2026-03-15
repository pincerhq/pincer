"""Tests for ImageProviderRouter — fallback behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pincer.image.provider_base import BaseImageProvider
from pincer.image.router import ImageProviderRouter
from pincer.image.types import GeneratedImage, ImageRequest, ImageResult


def _make_provider(available: bool, result=None, raises=None) -> BaseImageProvider:
    provider = MagicMock(spec=BaseImageProvider)
    provider.is_available.return_value = available
    if raises:
        provider.generate = AsyncMock(side_effect=raises)
    else:
        provider.generate = AsyncMock(return_value=result)
    return provider


@pytest.mark.asyncio
async def test_uses_first_available():
    good_result = ImageResult(
        images=[GeneratedImage(url="http://x.com/img.jpg")],
        provider="fal",
        model="m",
        cost_usd=0.003,
    )
    p1 = _make_provider(available=True, result=good_result)
    p2 = _make_provider(available=True, result=good_result)
    router = ImageProviderRouter([p1, p2])
    result = await router.generate(ImageRequest(prompt="x"))
    assert result.provider == "fal"
    p1.generate.assert_called_once()
    p2.generate.assert_not_called()


@pytest.mark.asyncio
async def test_fallback_to_second_on_failure():
    fallback_result = ImageResult(images=[GeneratedImage(bytes=b"PNG")], provider="gemini", model="g", cost_usd=0.004)
    p1 = _make_provider(available=True, raises=RuntimeError("fal failed"))
    p2 = _make_provider(available=True, result=fallback_result)
    router = ImageProviderRouter([p1, p2])
    result = await router.generate(ImageRequest(prompt="x"))
    assert result.provider == "gemini"


@pytest.mark.asyncio
async def test_skips_unavailable_provider():
    good_result = ImageResult(
        images=[GeneratedImage(url="http://x.com/img.jpg")],
        provider="gemini",
        model="g",
        cost_usd=0.004,
    )
    p1 = _make_provider(available=False)
    p2 = _make_provider(available=True, result=good_result)
    router = ImageProviderRouter([p1, p2])
    result = await router.generate(ImageRequest(prompt="x"))
    assert result.provider == "gemini"
    p1.generate.assert_not_called()


@pytest.mark.asyncio
async def test_raises_when_all_fail():
    p1 = _make_provider(available=True, raises=RuntimeError("fail1"))
    p2 = _make_provider(available=True, raises=RuntimeError("fail2"))
    router = ImageProviderRouter([p1, p2])
    with pytest.raises(RuntimeError, match="All image providers failed"):
        await router.generate(ImageRequest(prompt="x"))
