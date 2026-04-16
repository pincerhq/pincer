"""Cursor-based pagination helper for Slack list APIs."""

from __future__ import annotations

from typing import Any


async def paginate(
    client_method: Any,
    result_key: str,
    limit: int = 200,
    max_pages: int = 10,
    **kwargs: Any,
) -> list[Any]:
    """Fetch all pages from a cursor-paginated Slack API method.

    Args:
        client_method: Bound AsyncWebClient method (e.g. ``client.bot.conversations_list``).
        result_key: Top-level key containing the list (e.g. ``"channels"``).
        limit: Items per request (Slack max varies by endpoint; 200 is safe default).
        max_pages: Safety cap to prevent unbounded iteration.
        **kwargs: Extra keyword args forwarded to *client_method*.

    Returns:
        Flat list of all items across all pages.
    """
    items: list[Any] = []
    cursor: str | None = None
    pages = 0

    while pages < max_pages:
        params: dict[str, Any] = {"limit": limit, **kwargs}
        if cursor:
            params["cursor"] = cursor

        resp = await client_method(**params)
        batch = resp.get(result_key) or []
        items.extend(batch)
        pages += 1

        metadata = resp.get("response_metadata") or {}
        cursor = metadata.get("next_cursor") or ""
        if not cursor:
            break

    return items
