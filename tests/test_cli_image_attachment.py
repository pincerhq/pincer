"""Tests for _format_image_attachment — the file_path hint injected into the
LLM prompt for uploaded images, so an OCR-capable MCP tool has a concrete
argument to act on instead of the model trying to transcribe the image via
its own vision."""

from __future__ import annotations

from pincer.cli import _IMAGE_EXTENSIONS, _format_image_attachment


def test_hint_includes_saved_path_and_size() -> None:
    result = _format_image_attachment("image_ab12cd34.png", "/data/uploads/image_ab12cd34.png", 4096, "image/png")
    assert "/data/uploads/image_ab12cd34.png" in result
    assert "4096 bytes" in result
    assert "image/png" in result


def test_hint_tells_model_to_use_file_path_not_transcribe() -> None:
    result = _format_image_attachment("photo.jpg", "/data/uploads/photo.jpg", 100, "image/jpeg")
    assert "file_path='/data/uploads/photo.jpg'" in result
    assert "do not try to transcribe it yourself" in result


def test_common_image_media_types_map_to_expected_extensions() -> None:
    assert _IMAGE_EXTENSIONS["image/jpeg"] == ".jpg"
    assert _IMAGE_EXTENSIONS["image/png"] == ".png"
    assert _IMAGE_EXTENSIONS["image/webp"] == ".webp"


def test_unknown_media_type_falls_back_to_bin_extension() -> None:
    # cli.py looks this up as _IMAGE_EXTENSIONS.get(media_type, ".bin")
    assert _IMAGE_EXTENSIONS.get("image/heic", ".bin") == ".bin"
