"""
websearch_mcp — Web search MCP server built with the fastmcp library.

Primary: Tavily API (rich results, free tier 1000/month)
Fallback: DuckDuckGo via the ddgs package (no API key)

Transports:
    stdio (default):  python server.py --transport stdio
    HTTP:  python server.py --transport http [--host 0.0.0.0] [--port 8000]
        MCP endpoint → http://localhost:8000/mcp

    Or use the fastmcp CLI:
        fastmcp run server.py --transport http --port 8000

Environment:
    API_KEY   Your Tavily API key (optional — falls back to DuckDuckGo if unset).
                  Get a free key at https://app.tavily.com
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from typing import TYPE_CHECKING, Any

import httpx
import uvicorn
from fastapi.responses import JSONResponse
from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.cors import CORSMiddleware

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
API_KEY = os.environ.get("API_KEY", "")

# Configure logging
logging.basicConfig(level=getattr(logging, LOG_LEVEL), format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="websearch_mcp",
    instructions=(
        """
        Web Search MCP Server

        This server provides web search with Tavily as the primary provider
        and DuckDuckGo as an automatic fallback when Tavily is unavailable,
        unconfigured, or rate limited.

        Available tools:
        - search: search the web for a query and return a short answer
          summary (Tavily only) plus a list of results (title, url, snippet)

        Features:
        - Automatic provider fallback with retry/backoff on transient errors
        - Works without any API key (DuckDuckGo-only mode)
        """
    ),
)


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------


async def _retry_async[T](
    fn: Callable[[], Awaitable[T]],
    *,
    retryable: tuple[type[Exception], ...],
    retries: int = 2,
    base_delay: float = 0.5,
) -> T:
    """Retry fn() with exponential backoff (0.5s, 1.5s, ...) on *retryable* exceptions only."""
    for attempt in range(retries + 1):
        try:
            return await fn()
        except retryable:
            if attempt == retries:
                raise
            await asyncio.sleep(base_delay * (3**attempt))
    raise AssertionError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------------------
# Search providers
# ---------------------------------------------------------------------------


async def _search_tavily(query: str, num_results: int) -> dict[str, Any]:
    """Search using the Tavily API, with retry/backoff on transient errors."""
    from tavily import AsyncTavilyClient
    from tavily.errors import TimeoutError as TavilyTimeoutError
    from tavily.errors import UsageLimitExceededError

    client = AsyncTavilyClient(api_key=API_KEY)

    async def _call() -> dict[str, Any]:
        return await client.search(  # type: ignore[no-any-return]
            query=query,
            max_results=num_results,
            search_depth="basic",
            include_answer=True,
        )

    retryable = (TavilyTimeoutError, UsageLimitExceededError, httpx.TimeoutException)
    response = await _retry_async(_call, retryable=retryable)

    results = [
        {
            "title": r.get("title", "No title"),
            "url": r.get("url", ""),
            "snippet": (r.get("content") or "")[:300],
        }
        for r in response.get("results", [])
    ]
    return {"answer": response.get("answer"), "results": results}


def _sync_ddg_search(query: str, num_results: int) -> list[dict[str, str]]:
    """Run DuckDuckGo search synchronously (called via asyncio.to_thread)."""
    from ddgs import DDGS

    with DDGS() as ddgs:
        return list(ddgs.text(query, max_results=num_results))


async def _search_duckduckgo(query: str, num_results: int) -> dict[str, Any]:
    """Search using DuckDuckGo (no API key needed), with retry/backoff on transient errors."""
    from ddgs.exceptions import RatelimitException, TimeoutException

    async def _call() -> list[dict[str, str]]:
        return await asyncio.to_thread(_sync_ddg_search, query, num_results)

    raw_results = await _retry_async(_call, retryable=(RatelimitException, TimeoutException))

    results = [
        {
            "title": r.get("title", "No title"),
            "url": r.get("href", ""),
            "snippet": (r.get("body") or "")[:300],
        }
        for r in raw_results
    ]
    return {"answer": None, "results": results}


# ---------------------------------------------------------------------------
# Input model (Pydantic v2)
# ---------------------------------------------------------------------------


class SearchInput(BaseModel):
    """Parameters for a web search."""

    model_config = ConfigDict(
        populate_by_name=True,
        str_strip_whitespace=True,
        extra="forbid",
    )

    query: str = Field(..., description="The search query", max_length=500)
    num_results: int = Field(
        default=5,
        description="Number of results to return (1-10)",
        ge=1,
        le=10,
    )


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@mcp.custom_route("/", methods=["GET"])
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "healthy",
            "service": "websearch-mcp",
            "version": "0.0.1",
            "transport": "http",
        }
    )


# ---------------------------------------------------------------------------
# Tools  (fastmcp uses @mcp.tool without parentheses)
# ---------------------------------------------------------------------------


@mcp.tool(
    name="search",
    description="Search the web for a query. Tries Tavily first (if configured), falls back to DuckDuckGo.",
    tags={"search", "web"},
)
async def search(params: SearchInput) -> str:
    """
    Search the web and return a short answer plus a list of results.

    Args:
        params: query and num_results (1-10)

    Returns:
        JSON with an optional "answer" summary and a "results" list of
        {title, url, snippet}. On total failure, returns {"error": "..."}.
    """
    logger.info("tool: search")
    try:
        if API_KEY:
            try:
                data = await _search_tavily(params.query, params.num_results)
            except Exception as exc:
                logger.warning("Tavily search failed: %s, falling back to DuckDuckGo", exc)
                data = await _search_duckduckgo(params.query, params.num_results)
        else:
            data = await _search_duckduckgo(params.query, params.num_results)
        return json.dumps(data, indent=2)
    except Exception as exc:
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"}, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    try:
        from pincer_telemetry import init as _init_telemetry

        _init_telemetry(project_name="websearch_mcp", version="0.1.0")
        logger.info("Telemetry init success")
    except ImportError:
        logger.warning(
            "OTEL_DSN is set but pincer-telemetry is not installed — "
            'skipping. Install with: pip install "pincer-mcps[telemetry]"'
        )
    except Exception as _tel_err:
        logger.error("Telemetry init failed (non-fatal): %s", _tel_err)

    parser = argparse.ArgumentParser(description="Web search MCP server (fastmcp)")
    parser.add_argument(
        "--transport",
        choices=["http", "stdio"],
        default=os.environ.get("TRANSPORT", "stdio"),
        help="Transport: 'http' (streamable HTTP, default) or 'stdio'",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "0.0.0.0"),
        help="Bind host for HTTP transport (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=os.environ.get("PORT", 8000),
        help="Bind port for HTTP transport (default: 8000)",
    )
    args = parser.parse_args()

    if args.transport == "http":
        logger.info("Starting websearch_mcp · HTTP transport · %s:%s/mcp", args.host, args.port)
        app = mcp.http_app()
        cors_app = CORSMiddleware(
            app=app,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )
        uvicorn.run(cors_app, host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
