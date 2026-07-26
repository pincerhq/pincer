"""Tests for detect_text, calling the real FastMCP server with a mocked predictor."""

from __future__ import annotations

from typing import Any

import pytest


@pytest.mark.asyncio
async def test_detect_text_returns_boxes(fake_predictor_builder: list[Any], tiny_png_b64: str) -> None:
    from doctr_mcp.server import mcp

    result = await mcp.call_tool("detect_text", {"params": {"source": {"image_b64": tiny_png_b64}}})
    data = result.structured_content
    assert data is not None
    assert len(data["results"]) == 1
    boxes = data["results"][0]["boxes"]
    assert len(boxes) == 1
    assert boxes[0]["geometry"] == [0.1, 0.1, 0.2, 0.2]
    assert boxes[0]["confidence"] == 0.93


@pytest.mark.asyncio
async def test_detect_text_uses_det_arch_in_cache_key(fake_predictor_builder: list[Any], tiny_png_b64: str) -> None:
    from doctr_mcp.server import mcp

    await mcp.call_tool(
        "detect_text",
        {"params": {"source": {"image_b64": tiny_png_b64}, "det_arch": "fast_base"}},
    )
    assert fake_predictor_builder[-1].task == "detection"
    assert fake_predictor_builder[-1].det_arch == "fast_base"


@pytest.mark.asyncio
async def test_detect_text_rejects_both_sources(fake_predictor_builder: list[Any], tiny_png_b64: str) -> None:
    from doctr_mcp.server import mcp
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="exactly one"):
        await mcp.call_tool(
            "detect_text",
            {"params": {"source": {"image_b64": tiny_png_b64, "file_path": "/tmp/x.png"}}},
        )
