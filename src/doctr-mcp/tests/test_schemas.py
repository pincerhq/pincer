"""Tests for pydantic input model validation edge cases."""

from __future__ import annotations

import pytest
from doctr_mcp.schemas import DetectionInput, ImageSource, OcrDocumentInput
from pydantic import ValidationError


def test_image_source_requires_exactly_one() -> None:
    with pytest.raises(ValidationError):
        ImageSource()
    with pytest.raises(ValidationError):
        ImageSource(image_b64="Zm9v", file_path="/tmp/x.png")


def test_image_source_accepts_b64_only() -> None:
    source = ImageSource(image_b64="Zm9v")
    assert source.file_path is None


def test_image_source_accepts_file_path_only() -> None:
    source = ImageSource(file_path="/tmp/x.png")
    assert source.image_b64 is None


def test_detection_input_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        DetectionInput(source=ImageSource(image_b64="Zm9v"), unexpected_field=123)  # type: ignore[call-arg]


def test_detection_input_defaults_from_settings() -> None:
    params = DetectionInput(source=ImageSource(image_b64="Zm9v"))
    assert params.det_arch  # populated via default_factory, non-empty


def test_ocr_document_input_page_range_is_a_tuple() -> None:
    params = OcrDocumentInput(source=ImageSource(image_b64="Zm9v"), page_range=(0, 3))
    assert params.page_range == (0, 3)
