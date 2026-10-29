"""Decoding MCP ``CallToolResult``s for callers that use a tool's value, not its display text.

Shared by the MCP memory backend and the tool selector; the LLM-facing
formatting lives in ``bridge._format_result``.
"""

from __future__ import annotations

import json
from typing import Any


def first_text(result: Any) -> str:
    """Text of a result's first content item ("" when there is none)."""
    content = getattr(result, "content", None) or []
    return str(getattr(content[0], "text", "")) if content else ""


def raise_on_error(result: Any, what: str) -> Any:
    """Return ``result``, or raise RuntimeError("<what> failed: <text>") when the tool reported an error."""
    if getattr(result, "isError", False):
        raise RuntimeError(f"{what} failed: {first_text(result)}")
    return result


def result_json(result: Any) -> Any:
    """Decode the JSON value an MCP tool returned."""
    # fastmcp sends object return values as structuredContent and wraps any other
    # value as structuredContent={"result": ...}; prefer it over parsing text.
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured.get("result", structured)
    raw = first_text(result)
    # fastmcp serialises an empty list result as no content items at all.
    if not raw.strip():
        return None
    return json.loads(raw)
