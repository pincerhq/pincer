"""
Generic pageToken-based pagination helper for Google APIs.

Google list APIs return a ``nextPageToken`` field when there are more results.
Pass ``collect_pages`` a callable that accepts ``pageToken`` and returns a
response dict; it will keep fetching until exhausted or ``max_pages`` is hit.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


async def collect_pages(
    execute_fn: Callable[[str | None], Any],
    items_key: str = "items",
    max_pages: int = 10,
) -> list[Any]:
    """Collect all items across paginated Google API responses.

    Args:
        execute_fn: Callable(pageToken | None) → response dict.  Should call
            ``.execute()`` on the underlying API request, substituting the
            pageToken.  Run synchronously; wrapped in asyncio.to_thread here.
        items_key: The key in the response dict that holds the item list.
        max_pages: Safety cap — stop after this many pages.

    Returns:
        Flat list of all items across pages.
    """
    items: list[Any] = []
    page_token: str | None = None
    pages_fetched = 0

    while pages_fetched < max_pages:
        response = await asyncio.to_thread(execute_fn, page_token)
        pages_fetched += 1
        batch = response.get(items_key, []) or []
        items.extend(batch)
        page_token = response.get("nextPageToken")
        if not page_token:
            break
        logger.debug("Fetching page %d (token=%s…)", pages_fetched + 1, page_token[:12])

    if page_token:
        logger.debug("Pagination stopped at max_pages=%d; more results may exist", max_pages)

    return items
