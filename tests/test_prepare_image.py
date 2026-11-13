"""Preparing a photo for the model, once, on arrival (issue #358, slice 3)."""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import random

import pytest
from PIL import Image

from pincer.llm import images
from pincer.llm.images import MAX_ENCODED_BYTES, MAX_LONG_EDGE, prepare_image


def encode(image: Image.Image, fmt: str, **options) -> bytes:
    out = io.BytesIO()
    image.save(out, fmt, **options)
    return out.getvalue()


def striped(width: int, height: int, mode: str = "RGB") -> Image.Image:
    """Something that is not one flat colour, so encoders have work to do and rotation shows."""
    image = Image.new(mode, (width, height))
    pixels = image.load()
    assert pixels is not None
    for x in range(0, width, 16):
        for y in range(0, height, 16):
            value = (x % 256, y % 256, (x + y) % 256)
            pixels[x, y] = value if mode == "RGB" else (*value, 128 if x % 32 else 255)
    return image


def noisy(width: int, height: int, mode: str = "RGB") -> Image.Image:
    """Incompressible: its encoded size is big for its pixels."""
    rng = random.Random(7)
    channels = len(mode)
    return Image.frombytes(mode, (width, height), rng.randbytes(width * height * channels))


def size_of(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as image:
        return image.size


def test_a_4000_by_3000_jpeg_is_stored_at_a_2576_long_edge_with_its_aspect_kept():
    raw = encode(striped(4000, 3000), "JPEG", quality=90)

    prepared = prepare_image(raw, "image/jpeg")

    assert size_of(raw) == (4000, 3000)  # precondition
    assert size_of(prepared.data) == (2576, 1932)
    assert prepared.media_type == "image/jpeg"
    assert prepared.sha256 == hashlib.sha256(prepared.data).hexdigest()
    assert prepared.data != raw


def test_an_image_under_the_limit_is_kept_byte_for_byte():
    raw = encode(striped(1000, 800), "JPEG", quality=90)

    prepared = prepare_image(raw, "image/jpeg")

    assert prepared.data == raw
    assert prepared.media_type == "image/jpeg"
    assert prepared.sha256 == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("fmt", ["PNG", "GIF", "WEBP"])
def test_every_format_under_the_limit_is_kept_byte_for_byte(fmt):
    raw = encode(striped(600, 400).convert("RGB" if fmt != "GIF" else "P"), fmt)

    assert prepare_image(raw, f"image/{fmt.lower()}").data == raw


def test_the_limit_is_on_the_long_edge_and_exact():
    at_limit = encode(striped(MAX_LONG_EDGE, 1000), "JPEG")
    over_limit = encode(striped(MAX_LONG_EDGE + 1, 1000), "JPEG")
    tall = encode(striped(1000, MAX_LONG_EDGE + 1), "JPEG")

    assert prepare_image(at_limit, "image/jpeg").data == at_limit
    assert size_of(prepare_image(over_limit, "image/jpeg").data) == (MAX_LONG_EDGE, 1000)
    assert size_of(prepare_image(tall, "image/jpeg").data) == (1000, MAX_LONG_EDGE)


def test_preparing_the_output_again_returns_it_unchanged():
    raw = encode(striped(4000, 3000), "JPEG", quality=90)
    once = prepare_image(raw, "image/jpeg")

    twice = prepare_image(once.data, once.media_type)

    assert twice == once


def test_the_same_input_gives_the_same_bytes():
    raw = encode(striped(3000, 2000), "JPEG", quality=90)

    assert prepare_image(raw, "image/jpeg") == prepare_image(raw, "image/jpeg")


def test_a_phone_photo_is_turned_upright_before_its_orientation_tag_is_dropped():
    image = striped(4000, 3000)
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 degrees clockwise to display
    raw = encode(image, "JPEG", quality=90, exif=exif)
    assert size_of(raw) == (4000, 3000)  # precondition: stored sideways

    prepared = prepare_image(raw, "image/jpeg")

    assert size_of(prepared.data) == (1932, 2576)  # shown portrait
    with Image.open(io.BytesIO(prepared.data)) as out:
        assert out.getexif().get(0x0112) in (None, 1)


def test_an_oversized_png_with_alpha_stays_a_png_with_alpha():
    raw = encode(striped(3000, 2000, "RGBA"), "PNG")

    prepared = prepare_image(raw, "image/png")

    assert prepared.media_type == "image/png"
    with Image.open(io.BytesIO(prepared.data)) as out:
        assert out.size == (2576, 1717)
        assert out.mode == "RGBA"
        assert out.getchannel("A").getextrema() != (255, 255)  # precondition: transparency survived


def test_a_palette_png_with_transparency_counts_as_alpha():
    base = striped(3000, 2000).convert("P")
    raw = encode(base, "PNG", transparency=0)

    prepared = prepare_image(raw, "image/png")

    assert prepared.media_type == "image/png"
    with Image.open(io.BytesIO(prepared.data)) as out:
        assert out.mode == "RGBA"


def test_an_oversized_opaque_png_becomes_a_jpeg():
    raw = encode(striped(3000, 2000), "PNG")

    prepared = prepare_image(raw, "image/png")

    assert prepared.media_type == "image/jpeg"
    assert size_of(prepared.data) == (2576, 1717)


def test_an_oversized_webp_becomes_a_jpeg():
    raw = encode(striped(3000, 2000), "WEBP")

    prepared = prepare_image(raw, "image/webp")

    assert prepared.media_type == "image/jpeg"
    assert size_of(prepared.data) == (2576, 1717)


def test_the_byte_cap_keeps_the_base64_payload_under_the_smallest_documented_limit():
    """Amazon Bedrock and Google (Google managed): 5 MB per image, base64-encoded."""
    assert len(base64.b64encode(bytes(MAX_ENCODED_BYTES))) == 5_000_000
    assert len(base64.b64encode(bytes(MAX_ENCODED_BYTES + 1))) > 5_000_000


def test_a_small_image_that_is_too_heavy_is_re_encoded_once(monkeypatch):
    image = noisy(800, 600)
    raw = encode(image, "JPEG", quality=100)
    cap = (len(raw) + len(encode(image, "JPEG", quality=images.JPEG_QUALITY))) // 2  # between the two sizes
    monkeypatch.setattr(images, "MAX_ENCODED_BYTES", cap)
    assert len(raw) > cap  # precondition: over the cap, under the pixel limit

    prepared = prepare_image(raw, "image/jpeg")

    assert len(prepared.data) <= cap
    assert size_of(prepared.data) == (800, 600)
    assert prepare_image(prepared.data, prepared.media_type) == prepared  # and not again


def test_an_alpha_png_over_the_byte_cap_is_flattened_to_a_jpeg(monkeypatch):
    monkeypatch.setattr(images, "MAX_ENCODED_BYTES", 200_000)
    raw = encode(noisy(600, 400, "RGBA"), "PNG")
    assert len(raw) > 200_000  # precondition

    prepared = prepare_image(raw, "image/png")

    assert prepared.media_type == "image/jpeg"
    assert len(prepared.data) <= 200_000


def test_bytes_that_are_not_an_image_come_back_unchanged_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="pincer.llm.images"):
        prepared = prepare_image(b"%PDF not an image", "image/png")

    assert prepared.data == b"%PDF not an image"
    assert prepared.media_type == "image/png"
    assert prepared.sha256 == hashlib.sha256(b"%PDF not an image").hexdigest()
    assert [r.levelno for r in caplog.records if r.name == "pincer.llm.images"] == [logging.WARNING]


def test_an_oversized_image_that_cannot_be_decoded_comes_back_unchanged(caplog):
    whole = encode(striped(3000, 2000), "JPEG", quality=90)
    truncated = whole[: len(whole) // 3]

    with caplog.at_level(logging.WARNING, logger="pincer.llm.images"):
        prepared = prepare_image(truncated, "image/jpeg")

    assert prepared.data == truncated
    assert any(r.levelno == logging.WARNING for r in caplog.records)
