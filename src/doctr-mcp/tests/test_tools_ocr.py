"""Tests for ocr_document and ocr_document_text."""

from __future__ import annotations

from typing import Any

import pytest


@pytest.mark.asyncio
async def test_ocr_document_returns_full_tree(fake_predictor_builder: list[Any], tiny_png_b64: str) -> None:
    from doctr_mcp.server import mcp

    result = await mcp.call_tool("ocr_document", {"params": {"source": {"image_b64": tiny_png_b64}}})
    data = result.structured_content
    assert data is not None
    page = data["pages"][0]
    assert page["blocks"][0]["lines"][0]["words"][0]["value"] == "HELLO"
    assert page["blocks"][0]["lines"][0]["words"][0]["confidence"] == 0.95
    assert page["dimensions"] == [100, 200]


@pytest.mark.asyncio
async def test_ocr_document_text_returns_plain_text(fake_predictor_builder: list[Any], tiny_png_b64: str) -> None:
    from doctr_mcp.server import mcp

    result = await mcp.call_tool("ocr_document_text", {"params": {"source": {"image_b64": tiny_png_b64}}})
    data = result.structured_content
    assert data is not None
    assert data["pages"] == ["HELLO"]
    assert data["text"] == "HELLO"
    assert "geometry" not in data


@pytest.mark.asyncio
async def test_ocr_document_and_text_share_cached_predictor_for_same_params(
    fake_predictor_builder: list[Any], tiny_png_b64: str
) -> None:
    """Both tools build a PredictorParams(task='ocr', ...) — identical params should
    hit the same cache entry regardless of which tool is called first."""
    from doctr_mcp.server import mcp

    await mcp.call_tool("ocr_document", {"params": {"source": {"image_b64": tiny_png_b64}}})
    await mcp.call_tool("ocr_document_text", {"params": {"source": {"image_b64": tiny_png_b64}}})
    assert len(fake_predictor_builder) == 1


@pytest.mark.asyncio
async def test_ocr_document_passes_ocr_only_params(fake_predictor_builder: list[Any], tiny_png_b64: str) -> None:
    from doctr_mcp.server import mcp

    await mcp.call_tool(
        "ocr_document",
        {"params": {"source": {"image_b64": tiny_png_b64}, "resolve_blocks": True, "paragraph_break": 0.05}},
    )
    params = fake_predictor_builder[-1]
    assert params.task == "ocr"
    assert params.resolve_blocks is True
    assert params.paragraph_break == 0.05
