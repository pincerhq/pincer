"""Tests for generate_image builtin tool."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from pincer.image.router import ImageProviderRouter
from pincer.image.types import GeneratedImage, ImageResult
from pincer.tools.builtin.image_gen import make_generate_image_handler


def _make_router(result: ImageResult) -> ImageProviderRouter:
    router = MagicMock(spec=ImageProviderRouter)
    router.generate = AsyncMock(return_value=result)
    return router


def _make_channel():
    ch = MagicMock()
    ch.send_photo = AsyncMock()
    ch.send_photo_from_bytes = AsyncMock()
    return ch


@pytest.mark.asyncio
async def test_sends_url_image():
    channel = _make_channel()
    result = ImageResult(
        images=[GeneratedImage(url="https://cdn.fal.ai/img.jpg")],
        provider="fal",
        model="fal-ai/nano-banana-2",
        cost_usd=0.003,
    )
    router = _make_router(result)
    handler = make_generate_image_handler(router)

    ctx = {"user_id": "user123", "channel": "telegram", "channel_map": {"telegram": channel}}
    output = await handler(prompt="a sunset", context=ctx)

    data = json.loads(output)
    assert data["status"] == "success"
    assert data["images_sent"] == 1
    channel.send_photo.assert_called_once_with(
        "user123",
        "https://cdn.fal.ai/img.jpg",
        "",
    )


@pytest.mark.asyncio
async def test_sends_bytes_image():
    channel = _make_channel()
    result = ImageResult(
        images=[GeneratedImage(bytes=b"PNG_DATA", content_type="image/png")],
        provider="gemini",
        model="gemini-2.5-flash-image",
        cost_usd=0.004,
    )
    router = _make_router(result)
    handler = make_generate_image_handler(router)

    ctx = {"user_id": "user123", "channel": "telegram", "channel_map": {"telegram": channel}}
    output = await handler(prompt="a forest", caption="Nice!", context=ctx)

    data = json.loads(output)
    assert data["status"] == "success"
    channel.send_photo_from_bytes.assert_called_once_with("user123", b"PNG_DATA", "image/png", "Nice!")


@pytest.mark.asyncio
async def test_returns_error_without_channel():
    router = _make_router(ImageResult())
    handler = make_generate_image_handler(router)

    output = await handler(prompt="x", context={})
    data = json.loads(output)
    assert "error" in data


@pytest.mark.asyncio
async def test_returns_error_on_provider_failure():
    router = MagicMock(spec=ImageProviderRouter)
    router.generate = AsyncMock(side_effect=RuntimeError("all failed"))
    handler = make_generate_image_handler(router)

    channel = _make_channel()
    ctx = {"user_id": "u1", "channel": "telegram", "channel_map": {"telegram": channel}}
    output = await handler(prompt="x", context=ctx)
    data = json.loads(output)
    assert "error" in data


@pytest.mark.asyncio
async def test_discord_thread_routing():
    discord_channel = _make_channel()
    result = ImageResult(
        images=[GeneratedImage(url="https://cdn.fal.ai/img.jpg")],
        provider="fal",
        model="m",
        cost_usd=0.003,
    )
    router = _make_router(result)
    handler = make_generate_image_handler(router)

    ctx = {
        "user_id": "u1",
        "channel": "discord-thread-111222333",
        "channel_map": {"discord": discord_channel},
    }
    output = await handler(prompt="a robot", context=ctx)
    data = json.loads(output)
    assert data["status"] == "success"
    # Should pass thread_id kwarg
    discord_channel.send_photo.assert_called_once_with("u1", "https://cdn.fal.ai/img.jpg", "", thread_id="111222333")
