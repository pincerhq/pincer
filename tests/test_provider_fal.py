"""Tests for FalImageProvider."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pincer.image.provider_fal import FalImageProvider
from pincer.image.types import ImageRequest


@pytest.mark.asyncio
async def test_generate_returns_image_url():
    provider = FalImageProvider(api_key="fake-key")

    fake_result = {"images": [{"url": "https://cdn.fal.ai/image.jpg"}]}
    mock_fal = MagicMock()
    mock_fal.run_async = AsyncMock(return_value=fake_result)

    with patch.dict("sys.modules", {"fal_client": mock_fal}):
        result = await provider.generate(ImageRequest(prompt="a sunset"))

    assert len(result.images) == 1
    assert result.images[0].url == "https://cdn.fal.ai/image.jpg"
    assert result.provider == "fal"
    assert result.cost_usd > 0


@pytest.mark.asyncio
async def test_generate_raises_when_no_images():
    provider = FalImageProvider(api_key="fake-key")

    mock_fal = MagicMock()
    mock_fal.run_async = AsyncMock(return_value={"images": []})

    with (
        patch.dict("sys.modules", {"fal_client": mock_fal}),
        pytest.raises(RuntimeError, match="no images"),
    ):
        await provider.generate(ImageRequest(prompt="a sunset"))


def test_is_available_false_without_key():
    provider = FalImageProvider(api_key="")
    assert not provider.is_available()


def test_is_available_true_with_key_and_package():
    provider = FalImageProvider(api_key="fake-key")
    with patch.dict("sys.modules", {"fal_client": MagicMock()}):
        assert provider.is_available()


def test_estimate_cost():
    provider = FalImageProvider(api_key="fake-key")
    cost = provider.estimate_cost(ImageRequest(prompt="x", num_images=3))
    assert cost > 0
