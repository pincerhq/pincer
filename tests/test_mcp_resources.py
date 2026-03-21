"""Tests for MCP resource registry."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pincer.mcp.resources import ResourceRegistry, ResourceRegistration, _uri_to_name


# ── ResourceRegistry ──────────────────────────────────────────────────────────


def test_register_resource():
    registry = ResourceRegistry()
    handler = AsyncMock(return_value="data")
    registry.register(
        uri="pincer://memory/recent",
        handler=handler,
        description="Recent memory",
    )
    resources = registry.list_all()
    assert len(resources) == 1
    assert resources[0].uri == "pincer://memory/recent"
    assert resources[0].description == "Recent memory"
    assert resources[0].mime_type == "text/plain"


def test_register_resource_with_mime_type():
    registry = ResourceRegistry()
    handler = AsyncMock(return_value="<html/>")
    registry.register(
        uri="pincer://agent/status",
        handler=handler,
        description="Agent status",
        mime_type="text/html",
    )
    resources = registry.list_all()
    assert resources[0].mime_type == "text/html"


def test_list_all_empty():
    registry = ResourceRegistry()
    assert registry.list_all() == []


def test_list_all_multiple():
    registry = ResourceRegistry()
    for i in range(3):
        registry.register(
            uri=f"pincer://resource/{i}",
            handler=AsyncMock(return_value=str(i)),
            description=f"Resource {i}",
        )
    assert len(registry.list_all()) == 3


def test_export_to_fmcp_success():
    registry = ResourceRegistry()
    registry.register(
        uri="pincer://test",
        handler=AsyncMock(return_value="hello"),
        description="Test resource",
    )

    # Mock FastMCP instance with a resource decorator
    fmcp = MagicMock()
    decorator_fn = MagicMock(return_value=MagicMock())
    fmcp.resource = MagicMock(return_value=decorator_fn)

    count = registry.export_to(fmcp)
    assert count == 1
    fmcp.resource.assert_called_once()


def test_export_to_fmcp_no_resource_attr():
    registry = ResourceRegistry()
    registry.register(
        uri="pincer://test",
        handler=AsyncMock(return_value="x"),
        description="Test",
    )

    # FastMCP without .resource method — should gracefully skip
    fmcp = MagicMock(spec=[])  # no attributes
    count = registry.export_to(fmcp)
    assert count == 0


def test_export_to_fmcp_handler_error_graceful():
    """Export should succeed even if a specific resource fails to register."""
    registry = ResourceRegistry()
    # First resource will fail, second will succeed
    registry.register(uri="pincer://bad", handler=AsyncMock(), description="Bad")
    registry.register(uri="pincer://good", handler=AsyncMock(), description="Good")

    fmcp = MagicMock()
    call_count = 0

    def resource_side_effect(uri, **kwargs):
        nonlocal call_count
        call_count += 1
        if "bad" in uri:
            raise RuntimeError("bad resource")
        return MagicMock(return_value=MagicMock())

    fmcp.resource = MagicMock(side_effect=resource_side_effect)
    count = registry.export_to(fmcp)
    # The bad one fails, the good one succeeds
    assert count == 1


# ── _uri_to_name ──────────────────────────────────────────────────────────────


def test_uri_to_name_simple():
    name = _uri_to_name("pincer://memory/recent")
    assert "://" not in name
    assert "/" not in name


def test_uri_to_name_no_special_chars():
    name = _uri_to_name("pincer://a/b/c")
    assert name.isidentifier() or all(c.isalnum() or c == "_" for c in name)
