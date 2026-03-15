"""
Builtin generate_image tool — fal.ai Nano Banana (primary) + Gemini (fallback).

Replaces the generate_image skill. Sends images directly via channel.send_photo /
send_photo_from_bytes, following the same pattern as the legacy skill.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pincer.image.router import ImageProviderRouter

logger = logging.getLogger(__name__)


def _resolve_channel(channel_map: dict, ch_name: str):
    """Return (channel, send_kwargs) resolving Discord thread names."""
    channel = channel_map.get(ch_name)
    send_kwargs: dict[str, Any] = {}
    if channel is None and ch_name.startswith("discord-thread-"):
        channel = channel_map.get("discord")
        if channel:
            send_kwargs["thread_id"] = ch_name.replace("discord-thread-", "")
    return channel, send_kwargs


def make_generate_image_handler(router: ImageProviderRouter):
    """Return a `generate_image` async handler closed over the given router."""

    async def generate_image(
        prompt: str,
        caption: str = "",
        aspect_ratio: str = "1:1",
        num_images: int = 1,
        context: dict | None = None,
    ) -> str:
        """Generate image(s) from a text prompt and send them to the user.

        prompt: Detailed text description of the image to generate
        caption: Optional caption displayed with the image
        aspect_ratio: Image aspect ratio — 1:1 (default), 16:9, 9:16, 4:3, 3:4
        num_images: Number of images to generate (default 1, max 4)
        """
        from pincer.image.types import ImageRequest
        from pincer.llm.cost_tracker import get_cost_tracker

        ctx = context or {}
        channel_map = ctx.get("channel_map") or {}
        user_id = ctx.get("user_id", "")
        ch_name = ctx.get("channel", "")

        if not user_id or not channel_map:
            return json.dumps({"error": "No active channel — cannot send image"})

        channel, send_kwargs = _resolve_channel(channel_map, ch_name)
        if not channel:
            return json.dumps({"error": f"No active channel to send image (channel={ch_name})"})

        num_images = max(1, min(num_images, 4))

        try:
            request = ImageRequest(
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                num_images=num_images,
            )
            result = await router.generate(request)
        except Exception as e:
            logger.warning("Image generation failed: %s", e)
            return json.dumps({"error": f"Image generation failed: {e}"})

        sent = 0
        for img in result.images:
            try:
                if img.url:
                    await channel.send_photo(user_id, img.url, caption, **send_kwargs)
                elif img.bytes:
                    await channel.send_photo_from_bytes(user_id, img.bytes, img.content_type, caption, **send_kwargs)
                sent += 1
            except Exception as e:
                logger.warning("Failed to send image: %s", e)

        # Track image cost
        try:
            tracker = await get_cost_tracker()
            await tracker.add_image_cost(result.cost_usd, result.provider, result.model)
        except Exception as e:
            logger.debug("Cost tracking failed for image: %s", e)

        if sent == 0:
            return json.dumps({"error": "Failed to send any images to the user"})

        return json.dumps(
            {
                "status": "success",
                "provider": result.provider,
                "model": result.model,
                "images_sent": sent,
                "cost_usd": round(result.cost_usd, 6),
            }
        )

    return generate_image
