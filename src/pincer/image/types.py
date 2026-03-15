"""Shared types for the image generation module."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ImageRequest:
    prompt: str
    aspect_ratio: str = "1:1"
    num_images: int = 1
    model: str | None = None  # override provider default


@dataclass
class GeneratedImage:
    url: str | None = None       # fal returns a URL
    bytes: bytes | None = None   # Gemini returns raw bytes
    content_type: str = "image/png"


@dataclass
class ImageResult:
    images: list[GeneratedImage] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    cost_usd: float = 0.0
