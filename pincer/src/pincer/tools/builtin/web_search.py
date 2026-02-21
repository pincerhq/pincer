"""
Web search tool.

Primary: Tavily API (rich results, free tier 1000/month)
Fallback: DuckDuckGo via duckduckgo_search library (no API key)
"""

from __future__ import annotations

import asyncio
import logging

from pincer.config import get_settings

logger = logging.getLogger(__name__)


async def web_search(query: str, num_results: int = 5) -> str:
    """
    Search the web and return results.

    query: The search query string
    num_results: Number of results to return (1-10)
    """
    num_results = max(1, min(num_results, 10))

    settings = get_settings()
    tavily_key = settings.tavily_api_key.get_secret_value()

    if tavily_key:
        return await _search_tavily(query, num_results, tavily_key)
    return await _search_duckduckgo(query, num_results)


async def _search_tavily(query: str, num_results: int, api_key: str) -> str:
    """Search using Tavily API."""
    try:
        from tavily import AsyncTavilyClient

        client = AsyncTavilyClient(api_key=api_key)
        response = await client.search(
            query=query,
            max_results=num_results,
            search_depth="basic",
            include_answer=True,
        )
        parts: list[str] = []
        if response.get("answer"):
            parts.append(f"**Summary:** {response['answer']}\n")
        for i, result in enumerate(response.get("results", []), 1):
            title = result.get("title", "No title")
            url = result.get("url", "")
            snippet = result.get("content", "")[:300]
            parts.append(f"{i}. **{title}**\n   {snippet}\n   URL: {url}")
        return "\n\n".join(parts) if parts else "No results found."
    except ImportError:
        logger.warning("tavily-python not installed, falling back to DuckDuckGo")
        return await _search_duckduckgo(query, num_results)
    except Exception as e:
        logger.warning("Tavily search failed: %s, falling back to DuckDuckGo", e)
        return await _search_duckduckgo(query, num_results)


def _sync_ddg_search(query: str, num_results: int) -> list[dict[str, str]]:
    """Run DuckDuckGo search synchronously (called via asyncio.to_thread)."""
    from duckduckgo_search import DDGS

    with DDGS() as ddgs:
        return list(ddgs.text(query, max_results=num_results))


async def _search_duckduckgo(query: str, num_results: int) -> str:
    """Search using DuckDuckGo (no API key needed)."""
    try:
        results = await asyncio.to_thread(_sync_ddg_search, query, num_results)

        if not results:
            return "No results found."

        parts: list[str] = []
        for i, r in enumerate(results, 1):
            title = r.get("title", "No title")
            body = r.get("body", "")[:300]
            href = r.get("href", "")
            parts.append(f"{i}. **{title}**\n   {body}\n   URL: {href}")
        return "\n\n".join(parts)
    except ImportError:
        return (
            "Error: No search provider available. "
            "Install duckduckgo-search or set PINCER_TAVILY_API_KEY."
        )
    except Exception as e:
        return f"Search error: {e}"
