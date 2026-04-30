"""
Microsoft OneNote tools — 5 tools for notebook management.
"""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from pincer.integrations.ms365.graph_client import GraphClient
    from pincer.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


async def onenote__list_notebooks(
    client: GraphClient,
    max_results: int = 25,
) -> str:
    """List OneNote notebooks."""
    data = await client.get("/me/onenote/notebooks", params={"$top": str(max_results)})
    notebooks = data.get("value", [])
    if not notebooks:
        return "No notebooks found."
    lines = []
    for nb in notebooks:
        lines.append(
            f"  {nb.get('displayName', '?')} — modified {nb.get('lastModifiedDateTime', '')}\n"
            f"    ID: {nb.get('id', '')}"
        )
    return f"{len(lines)} notebook(s):\n" + "\n\n".join(lines)


async def onenote__list_sections(
    client: GraphClient,
    notebook_id: str,
) -> str:
    """List sections in a OneNote notebook."""
    data = await client.get(f"/me/onenote/notebooks/{notebook_id}/sections")
    sections = data.get("value", [])
    if not sections:
        return "No sections found."
    lines = []
    for s in sections:
        lines.append(
            f"  {s.get('displayName', '?')} — modified {s.get('lastModifiedDateTime', '')}\n    ID: {s.get('id', '')}"
        )
    return f"{len(lines)} section(s):\n" + "\n\n".join(lines)


async def onenote__list_pages(
    client: GraphClient,
    section_id: str,
    max_results: int = 50,
) -> str:
    """List pages in a OneNote section."""
    data = await client.get(
        f"/me/onenote/sections/{section_id}/pages",
        params={"$top": str(max_results), "$orderby": "lastModifiedDateTime desc"},
    )
    pages = data.get("value", [])
    if not pages:
        return "No pages found."
    lines = []
    for p in pages:
        lines.append(
            f"  {p.get('title', '(untitled)')} — modified {p.get('lastModifiedDateTime', '')}\n"
            f"    ID: {p.get('id', '')}"
        )
    return f"{len(lines)} page(s):\n" + "\n\n".join(lines)


async def onenote__get_page_content(
    client: GraphClient,
    page_id: str,
) -> str:
    """Get page content as HTML."""
    content = await client.get_binary(f"/me/onenote/pages/{page_id}/content")
    html = content.decode("utf-8", errors="replace")
    # Strip HTML for readability
    import re

    text = re.sub(r"<[^>]+>", "", html)
    text = re.sub(r"\s+", " ", text).strip()
    return f"Page content:\n{text[:5000]}"


async def onenote__create_page(
    client: GraphClient,
    section_id: str,
    title: str,
    content: str,
) -> str:
    """Create a new OneNote page with HTML content."""
    html_body = f"<!DOCTYPE html><html><head><title>{title}</title></head><body><p>{content}</p></body></html>"
    # OneNote pages require text/html content type
    result = await client.put(
        f"/me/onenote/sections/{section_id}/pages",
        content=html_body.encode("utf-8"),
        content_type="text/html",
    )
    return f"Page created: '{title}' (id={result.get('id', '')})"


# ── Registry ──────────────────────────────────────────────────────────────────


def register_onenote_tools(registry: ToolRegistry, client: GraphClient) -> int:
    """Register all 5 OneNote tools. Returns count."""

    def _h(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def wrapper(**kwargs: Any) -> str:
            return await fn(client, **kwargs)

        return wrapper

    registry.register(
        name="onenote__list_notebooks",
        description="List Microsoft OneNote notebooks.",
        handler=_h(onenote__list_notebooks),
        parameters={
            "type": "object",
            "properties": {"max_results": {"type": "integer", "default": 25}},
            "required": [],
        },
    )
    registry.register(
        name="onenote__list_sections",
        description="List sections in a OneNote notebook.",
        handler=_h(onenote__list_sections),
        parameters={
            "type": "object",
            "properties": {"notebook_id": {"type": "string"}},
            "required": ["notebook_id"],
        },
    )
    registry.register(
        name="onenote__list_pages",
        description="List pages in a OneNote section.",
        handler=_h(onenote__list_pages),
        parameters={
            "type": "object",
            "properties": {
                "section_id": {"type": "string"},
                "max_results": {"type": "integer", "default": 50},
            },
            "required": ["section_id"],
        },
    )
    registry.register(
        name="onenote__get_page_content",
        description="Get the content of a OneNote page.",
        handler=_h(onenote__get_page_content),
        parameters={
            "type": "object",
            "properties": {"page_id": {"type": "string"}},
            "required": ["page_id"],
        },
    )
    registry.register(
        name="onenote__create_page",
        description="Create a new OneNote page.",
        handler=_h(onenote__create_page),
        parameters={
            "type": "object",
            "properties": {
                "section_id": {"type": "string"},
                "title": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["section_id", "title", "content"],
        },
        require_approval=True,
    )
    return 5
