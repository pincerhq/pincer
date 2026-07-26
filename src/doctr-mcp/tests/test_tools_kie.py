"""Tests for extract_key_info."""

from __future__ import annotations

from typing import Any

import pytest


@pytest.mark.asyncio
async def test_extract_key_info_groups_by_class(fake_predictor_builder: list[Any], tiny_png_b64: str) -> None:
    from doctr_mcp.server import mcp

    result = await mcp.call_tool("extract_key_info", {"params": {"source": {"image_b64": tiny_png_b64}}})
    data = result.structured_content
    assert data is not None
    page = data["pages"][0]
    assert page["predictions"][0]["class_name"] == "total_price"
    assert page["predictions"][0]["items"][0]["value"] == "42.00"


@pytest.mark.asyncio
async def test_extract_key_info_uses_kie_task(fake_predictor_builder: list[Any], tiny_png_b64: str) -> None:
    from doctr_mcp.server import mcp

    await mcp.call_tool("extract_key_info", {"params": {"source": {"image_b64": tiny_png_b64}}})
    assert fake_predictor_builder[-1].task == "kie"
