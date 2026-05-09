"""
newsapi_mcp — NewsAPI.org MCP server built with the fastmcp library.

Transports:
    stdio (default):  python server.py --transport stdio
    HTTP:  python server.py --transport http [--host 0.0.0.0] [--port 8000]
        MCP endpoint → http://localhost:8000/mcp

    Or use the fastmcp CLI:
        fastmcp run server.py --transport http --port 8000

Environment:
    API_KEY   Your NewsAPI.org API key (required).
                  Get a free key at https://newsapi.org/register
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import httpx
import uvicorn
from fastapi.responses import JSONResponse
from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.cors import CORSMiddleware

if TYPE_CHECKING:
    from starlette.requests import Request

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
NEWSAPI_BASE_URL = "https://newsapi.org/v2"
API_KEY = os.environ.get("API_KEY", "")

VALID_CATEGORIES = [
    "business",
    "entertainment",
    "general",
    "health",
    "science",
    "sports",
    "technology",
]
VALID_LANGUAGES = [
    "ar",
    "de",
    "en",
    "es",
    "fr",
    "he",
    "it",
    "nl",
    "no",
    "pt",
    "ru",
    "sv",
    "ud",
    "zh",
]
VALID_COUNTRIES = [
    "ae",
    "ar",
    "at",
    "au",
    "be",
    "bg",
    "br",
    "ca",
    "ch",
    "cn",
    "co",
    "cu",
    "cz",
    "de",
    "eg",
    "fr",
    "gb",
    "gr",
    "hk",
    "hu",
    "id",
    "ie",
    "il",
    "in",
    "it",
    "jp",
    "kr",
    "lt",
    "lv",
    "ma",
    "mx",
    "my",
    "ng",
    "nl",
    "no",
    "nz",
    "ph",
    "pl",
    "pt",
    "ro",
    "rs",
    "ru",
    "sa",
    "se",
    "sg",
    "si",
    "sk",
    "th",
    "tr",
    "tw",
    "ua",
    "us",
    "ve",
    "za",
]

# Configure logging
logging.basicConfig(level=getattr(logging, LOG_LEVEL), format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="newsapi_mcp",
    instructions=(
        """
        NewsAPI.org MCP Server

        This server provides provides access to NewsAPI.org for searching news
        articles, fetching top headlines, and listing publishers.

        Available tools:
        - newsapi_search_everything: ull-text search across 150,000+ sources
          (last 5 years). Supports boolean operators, domain filters, and date
          ranges
        - newsapi_top_headlines: live breaking headlines filtered by country,
          category, or specific sources
        - newsapi_list_sources: discover valid source IDs before using the
          sources

        Features:
        - Multi-language support
        - Rate limit awareness (100 calls/day free tier)
        """
    ),
)


# ---------------------------------------------------------------------------
# Shared HTTP helpers
# ---------------------------------------------------------------------------


async def _newsapi_get(endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    """Authenticated GET request to NewsAPI.org. Strips None values."""
    if not API_KEY:
        raise ValueError(
            "API_KEY is not set. "
            "Register at https://newsapi.org/register to get a free API key, "
            "then export it: export API_KEY=your_key"
        )
    clean = {k: v for k, v in params.items() if v is not None}
    clean["apiKey"] = API_KEY
    async with httpx.AsyncClient(timeout=15.0) as client:
        logger.info(f"Requesting {NEWSAPI_BASE_URL}/{endpoint}")
        r = await client.get(f"{NEWSAPI_BASE_URL}/{endpoint}", params=clean)
        r.raise_for_status()
        return r.json()  # type: ignore[no-any-return]


def _handle_error(exc: Exception) -> str:
    """Map exceptions to clear, actionable error messages."""
    if isinstance(exc, ValueError):
        return f"Configuration error: {exc}"
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        try:
            msg = exc.response.json().get("message", "")
        except Exception:
            msg = ""
        if code == 401:
            return "Error: Invalid API key – check your API_KEY environment variable."
        if code == 426:
            return "Error: Your NewsAPI plan does not support this parameter. Upgrade at newsapi.org."
        if code == 429:
            return "Error: Rate limit exceeded. The free tier allows 100 requests/day."
        return f"Error: NewsAPI returned HTTP {code}. {msg}".strip()
    if isinstance(exc, httpx.TimeoutException):
        return "Error: Request timed out – please try again."
    return f"Error: {type(exc).__name__}: {exc}"


def _format_articles(articles: list[dict[str, Any]], total: int, offset: int) -> str:
    """Serialise a list of raw NewsAPI article dicts to compact JSON."""
    items = [
        {
            "title": a.get("title"),
            "source": a.get("source", {}).get("name"),
            "source_id": a.get("source", {}).get("id"),
            "author": a.get("author"),
            "description": a.get("description"),
            "url": a.get("url"),
            "image_url": a.get("urlToImage"),
            "published_at": a.get("publishedAt"),
        }
        for a in articles
    ]
    return json.dumps(
        {
            "total_results": total,
            "returned": len(items),
            "offset": offset,
            "has_more": total > offset + len(items),
            "articles": items,
        },
        indent=2,
    )


# ---------------------------------------------------------------------------
# Input models (Pydantic v2)
# ---------------------------------------------------------------------------


class SortBy(StrEnum):
    RELEVANCY = "relevancy"
    POPULARITY = "popularity"
    PUBLISHED_AT = "publishedAt"


class SearchEverythingInput(BaseModel):
    """Parameters for searching all NewsAPI articles."""

    model_config = ConfigDict(
        populate_by_name=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        extra="forbid",
    )

    q: str | None = Field(
        default=None,
        description=(
            "Keyword / phrase query. Supports AND, OR, NOT and quoted phrases. "
            'Example: "electric cars" AND (Tesla OR Rivian) NOT recall'
        ),
        max_length=500,
    )
    q_in_title: str | None = Field(
        default=None,
        alias="qInTitle",
        description="Keywords that must appear in the article title only.",
        max_length=500,
    )
    sources: str | None = Field(
        default=None,
        description=(
            "Comma-separated source IDs (max 20). "
            "Use newsapi_list_sources to get valid IDs. "
            "Cannot be combined with language or country."
        ),
    )
    domains: str | None = Field(
        default=None,
        description="Comma-separated domains to include, e.g. 'bbc.co.uk,techcrunch.com'.",
    )
    exclude_domains: str | None = Field(
        default=None,
        alias="excludeDomains",
        description="Comma-separated domains to exclude from results.",
    )
    # TODO: manage from_date and to_date cause they included in paid plan
    # from_date: Optional[str] = Field(
    #     default=None,
    #     alias="from",
    #     description="Oldest article date in ISO 8601 format, e.g. '2024-03-01'.",
    # )
    # to_date: Optional[str] = Field(
    #     default=None,
    #     alias="to",
    #     description="Newest article date in ISO 8601 format.",
    # )
    language: str | None = Field(
        default="en",
        description=f"2-letter language code. Options: {', '.join(VALID_LANGUAGES)}.",
    )
    sort_by: SortBy = Field(
        default=SortBy.PUBLISHED_AT,
        alias="sortBy",
        description="Sort order: 'relevancy' | 'popularity' | 'publishedAt' (default).",
    )
    page_size: int = Field(
        default=20,
        alias="pageSize",
        description="Results per page (1-100).",
        ge=1,
        le=100,
    )
    page: int = Field(
        default=1,
        description="Page number for pagination (default 1).",
        ge=1,
    )

    @field_validator("language")
    @classmethod
    def _check_language(cls, v: str | None) -> str | None:
        if v and v not in VALID_LANGUAGES:
            raise ValueError(f"'{v}' is not a valid language code. Options: {', '.join(VALID_LANGUAGES)}")
        return v


class TopHeadlinesInput(BaseModel):
    """Parameters for fetching top/breaking headlines."""

    model_config = ConfigDict(
        populate_by_name=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        extra="forbid",
    )

    q: str | None = Field(
        default=None,
        description="Keyword filter for headlines.",
        max_length=500,
    )
    sources: str | None = Field(
        default=None,
        description=("Comma-separated source IDs (max 20). Cannot be combined with country or category."),
    )
    country: str | None = Field(
        default=None,
        description=("2-letter ISO 3166-1 country code, e.g. 'us', 'de', 'gb'. Cannot be combined with sources."),
    )
    category: str | None = Field(
        default=None,
        description=f"News category. Options: {', '.join(VALID_CATEGORIES)}.",
    )
    page_size: int = Field(
        default=20,
        alias="pageSize",
        description="Results per page (1-100).",
        ge=1,
        le=100,
    )
    page: int = Field(
        default=1,
        description="Page number for pagination (default 1).",
        ge=1,
    )

    @field_validator("country")
    @classmethod
    def _check_country(cls, v: str | None) -> str | None:
        if v and v not in VALID_COUNTRIES:
            raise ValueError(f"'{v}' is not a valid country code.")
        return v

    @field_validator("category")
    @classmethod
    def _check_category(cls, v: str | None) -> str | None:
        if v and v not in VALID_CATEGORIES:
            raise ValueError(f"'{v}' is not valid. Options: {', '.join(VALID_CATEGORIES)}")
        return v


class ListSourcesInput(BaseModel):
    """Parameters for listing available NewsAPI publishers."""

    model_config = ConfigDict(
        populate_by_name=True,
        str_strip_whitespace=True,
        validate_assignment=True,
        extra="forbid",
    )

    category: str | None = Field(
        default=None,
        description=f"Filter by category. Options: {', '.join(VALID_CATEGORIES)}.",
    )
    language: str | None = Field(
        default=None,
        description=f"Filter by language code. Options: {', '.join(VALID_LANGUAGES)}.",
    )
    country: str | None = Field(
        default=None,
        description="Filter by country (2-letter ISO 3166-1 code).",
    )

    @field_validator("category")
    @classmethod
    def _check_category(cls, v: str | None) -> str | None:
        if v and v not in VALID_CATEGORIES:
            raise ValueError(f"'{v}' is not valid. Options: {', '.join(VALID_CATEGORIES)}")
        return v

    @field_validator("language")
    @classmethod
    def _check_language(cls, v: str | None) -> str | None:
        if v and v not in VALID_LANGUAGES:
            raise ValueError(f"'{v}' is not a valid language code. Options: {', '.join(VALID_LANGUAGES)}")
        return v

    @field_validator("country")
    @classmethod
    def _check_country(cls, v: str | None) -> str | None:
        if v and v not in VALID_COUNTRIES:
            raise ValueError(f"'{v}' is not a valid country code.")
        return v


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@mcp.custom_route("/", methods=["GET"])
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "healthy",
            "service": "newsapi-mcp",
            "version": "0.0.1",
            "transport": "http",
        }
    )


# ---------------------------------------------------------------------------
# Tools  (fastmcp uses @mcp.tool without parentheses)
# ---------------------------------------------------------------------------


@mcp.tool(
    name="search_everything",
    description="Search millions of news articles across sources published in the last 5 years.",
    tags={"news", "search"},
)
async def search_everything(params: SearchEverythingInput) -> str:
    """
    Search millions of news articles across sources published in the last 5 years.

    Args:
        params: structured query params with fields q, sources, country, category, page_size, page

    Returns:
        JSON with total_results, returned, offset, has_more, and an articles list.
        Each article has: title, source, source_id, author, description, url, image_url, published_at.
    """
    try:
        data = await _newsapi_get(
            "everything",
            {
                "q": params.q,
                "qInTitle": params.q_in_title,
                "sources": params.sources,
                "domains": params.domains,
                "excludeDomains": params.exclude_domains,
                "language": params.language,
                "sortBy": params.sort_by.value,
                "pageSize": params.page_size,
                "page": params.page,
            },
        )
        # data = await _newsapi_get(
        #    "everything",
        #    {
        #        "q": q
        #    },
        # )
        offset = (params.page - 1) * params.page_size
        return _format_articles(data.get("articles", []), data.get("totalResults", 0), offset)
    except Exception as exc:
        return _handle_error(exc)


@mcp.tool
async def top_headlines(params: TopHeadlinesInput) -> str:
    """Fetch live top and breaking headlines by country, category, or source.

    Use this for real-time news monitoring and news tickers.
    Note: 'sources' cannot be combined with 'country' or 'category'.

    Returns JSON with: total_results, returned, offset, has_more, and an articles list.
    Each article has: title, source, source_id, author, description, url, image_url, published_at.
    """
    try:
        data = await _newsapi_get(
            "top-headlines",
            {
                "q": params.q,
                "sources": params.sources,
                "country": params.country,
                "category": params.category,
                "pageSize": params.page_size,
                "page": params.page,
            },
        )
        offset = (params.page - 1) * params.page_size
        return _format_articles(data.get("articles", []), data.get("totalResults", 0), offset)
    except Exception as exc:
        return _handle_error(exc)


@mcp.tool
async def list_sources(params: ListSourcesInput) -> str:
    """List all publishers available for top headlines, with optional filtering.

    Use this to find valid source IDs before passing them to the 'sources'
    parameter in newsapi_top_headlines or newsapi_search_everything.

    Returns JSON with count and a sources list.
    Each source has: id, name, description, url, category, language, country.
    """
    try:
        data = await _newsapi_get(
            "top-headlines/sources",
            {
                "category": params.category,
                "language": params.language,
                "country": params.country,
            },
        )
        sources = [
            {
                "id": s.get("id"),
                "name": s.get("name"),
                "description": s.get("description"),
                "url": s.get("url"),
                "category": s.get("category"),
                "language": s.get("language"),
                "country": s.get("country"),
            }
            for s in data.get("sources", [])
        ]
        return json.dumps({"count": len(sources), "sources": sources}, indent=2)
    except Exception as exc:
        return _handle_error(exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="NewsAPI.org MCP server (fastmcp)")
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
        print(f"Starting newsapi_mcp · HTTP transport · {args.host}:{args.port}/mcp")
        app = mcp.http_app()
        cors_app = CORSMiddleware(
            app=app,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )
        uvicorn.run(cors_app, host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
