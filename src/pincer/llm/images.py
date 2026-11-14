"""Preparing a photo for the model, once, when it arrives (issue #358).

A photo stays in the conversation and goes out again on every request, so it is made
the size the model actually sees, and never touched again: the bytes stored are the
bytes sent, which keeps them a prompt-cache hit.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from PIL import Image, ImageOps

# The long edge the high-resolution tier of Claude 4.7+ and 5.x reads; also under the 3,000 px
# that every image must stay under once a request holds more than 20 of them
MAX_LONG_EDGE = 2576
JPEG_QUALITY = 85
# Providers cap an image's base64 size: 10 MB on the Claude API, 5 MB on Amazon Bedrock and
# Google (Google managed) (docs: build-with-claude/vision, "Request limits"). Base64 is 4 bytes
# for every 3, so 3,750,000 raw bytes is exactly 5,000,000 characters.
MAX_ENCODED_BYTES = 3_750_000

# The formats Claude and OpenAI take, by the name Pillow gives them, with their media type
_API_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif", "WEBP": "image/webp"}
_OPAQUE_MEDIA_TYPE = "image/jpeg"
_ALPHA_MEDIA_TYPE = "image/png"
_ALPHA_MODES = ("RGBA", "LA", "PA")
_WHITE = (255, 255, 255)


class UnreadableImage(ValueError):
    """Bytes no model can be shown: not an image Pillow decodes (a format it lacks, a corrupt or cut-short
    file, a decompression bomb)."""


@dataclass(frozen=True, slots=True)
class PreparedImage:
    """A photo as it is stored and sent: its bytes, their media type and their SHA-256 (hex)."""

    data: bytes
    media_type: str
    sha256: str


def prepare_image(raw: bytes) -> PreparedImage:
    """The photo as the conversation keeps it.

    An image of a format the models take (JPEG, PNG, GIF, WEBP) within the long-edge limit and the byte
    cap keeps its original bytes, and gets the media type of those bytes, whatever the channel claimed:
    a provider rejects a request whose image does not match its media type, and a photo that stays in the
    history would fail every later request. Any other is turned upright, scaled down (never up) to the
    limit and encoded once: PNG when it has transparency and the PNG fits the cap, otherwise JPEG. The
    result is deterministic, and preparing it again changes nothing. The whole image is decoded, so a
    file that is cut short is found here and not by the provider. Raises `UnreadableImage` for bytes that
    are not an image, or cannot be decoded. CPU-bound: call it off the event loop.
    """
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            media_type = _API_FORMATS.get(image.format or "")
            if media_type and max(image.size) <= MAX_LONG_EDGE and len(raw) <= MAX_ENCODED_BYTES:
                return _prepared(raw, media_type)
            return _re_encoded(image)
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as e:
        raise UnreadableImage(f"not an image the models can be shown ({len(raw)} bytes): {e}") from e


def _prepared(data: bytes, media_type: str) -> PreparedImage:
    return PreparedImage(data, media_type, hashlib.sha256(data).hexdigest())


def _re_encoded(image: Image.Image) -> PreparedImage:
    icc = image.info.get("icc_profile")
    upright = ImageOps.exif_transpose(image)
    width, height = upright.size
    if max(width, height) > MAX_LONG_EDGE:
        scale = MAX_LONG_EDGE / max(width, height)
        size = (
            (MAX_LONG_EDGE, max(1, round(height * scale)))
            if width >= height
            else (max(1, round(width * scale)), MAX_LONG_EDGE)
        )
        upright = upright.resize(size, Image.Resampling.LANCZOS)
    options = {"icc_profile": icc} if icc else {}
    if upright.mode in _ALPHA_MODES or "transparency" in upright.info:
        rgba = upright.convert("RGBA")
        png = _encode(rgba, "PNG", **options)
        if len(png) <= MAX_ENCODED_BYTES:
            return _prepared(png, _ALPHA_MEDIA_TYPE)
        flat = Image.new("RGB", rgba.size, _WHITE)
        flat.paste(rgba, mask=rgba.getchannel("A"))
        return _prepared(_encode(flat, "JPEG", quality=JPEG_QUALITY, **options), _OPAQUE_MEDIA_TYPE)
    return _prepared(_encode(upright.convert("RGB"), "JPEG", quality=JPEG_QUALITY, **options), _OPAQUE_MEDIA_TYPE)


def _encode(image: Image.Image, fmt: str, **options: object) -> bytes:
    out = io.BytesIO()
    image.save(out, fmt, **options)
    return out.getvalue()
