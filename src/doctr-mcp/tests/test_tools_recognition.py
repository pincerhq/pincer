"""Tests for recognize_text."""

from __future__ import annotations

from typing import Any

import pytest


@pytest.mark.asyncio
async def test_recognize_text_returns_one_result_per_image(
    fake_predictor_builder: list[Any], tiny_png_b64: str
) -> None:
    from doctr_mcp.server import mcp

    result = await mcp.call_tool(
        "recognize_text",
        {"params": {"images": [{"image_b64": tiny_png_b64}, {"image_b64": tiny_png_b64}]}},
    )
    data = result.structured_content
    assert data is not None
    assert len(data["results"]) == 2
    assert data["results"][0] == {"text": "HELLO", "confidence": 0.95}


@pytest.mark.asyncio
async def test_recognize_text_uses_reco_arch(fake_predictor_builder: list[Any], tiny_png_b64: str) -> None:
    from doctr_mcp.server import mcp

    await mcp.call_tool(
        "recognize_text",
        {"params": {"images": [{"image_b64": tiny_png_b64}], "reco_arch": "parseq"}},
    )
    assert fake_predictor_builder[-1].task == "recognition"
    assert fake_predictor_builder[-1].reco_arch == "parseq"
