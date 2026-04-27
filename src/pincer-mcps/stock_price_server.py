"""
stock_price_mcp — Stock and crypto price MCP server built with fastmcp.

Transports:
    stdio (default):  python server.py --transport stdio
    HTTP:             python server.py --transport http [--host 0.0.0.0] [--port 8000]
        MCP endpoint → http://localhost:8000/mcp

    Or use the fastmcp CLI:
        fastmcp run server.py --transport http --port 8000
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from typing import TYPE_CHECKING

import httpx
import uvicorn
from fastapi.responses import JSONResponse
from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.cors import CORSMiddleware

if TYPE_CHECKING:
    from starlette.requests import Request

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="stock_price_mcp",
    instructions=(
        "Stock and crypto price MCP server. "
        "Use get_stock_price to fetch current equity prices via Yahoo Finance, "
        "and get_crypto_price to fetch cryptocurrency prices via CoinGecko."
    ),
)

_YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"
_COINGECKO_URL = (
    "https://api.coingecko.com/api/v3/simple/price"
    "?ids={coin}&vs_currencies={currency}"
    "&include_24hr_change=true&include_market_cap=true"
)
_HEADERS = {"User-Agent": "Pincer/1.0"}


def _handle_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP error {exc.response.status_code}: invalid symbol or unavailable"
    if isinstance(exc, httpx.TimeoutException):
        return "Error: request timed out – please try again"
    return f"Error: {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


class StockInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    ticker: str = Field(description="Stock ticker symbol, e.g. AAPL, MSFT, TSLA")

    @field_validator("ticker")
    @classmethod
    def _not_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("ticker cannot be empty")
        return v.upper()


class CryptoInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    coin: str = Field(
        default="bitcoin",
        description="CoinGecko coin ID, e.g. bitcoin, ethereum, solana",
    )
    currency: str = Field(
        default="usd",
        description="Target fiat currency code, e.g. usd, eur, gbp",
    )

    @field_validator("coin", "currency")
    @classmethod
    def _not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("value cannot be empty")
        return v.strip().lower()


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@mcp.custom_route("/", methods=["GET"])
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "healthy",
            "service": "stock-price-mcp",
            "version": "0.1.0",
            "transport": "http",
        }
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool
async def get_stock_price(params: StockInput) -> str:
    """Get current stock price from Yahoo Finance.

    Returns ticker, price, previous_close, change, change_percent, currency, and name.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_HEADERS) as client:
            r = await client.get(_YAHOO_URL.format(ticker=params.ticker))
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        return _handle_error(exc)

    try:
        result = data.get("chart", {}).get("result")
        if not result:
            return json.dumps({"error": "invalid ticker or no data"})
        meta = result[0].get("meta", {})
        reg_price = meta.get("regularMarketPrice")
        if reg_price is None:
            return json.dumps({"error": "no price data for ticker"})
        price = float(reg_price)
        prev = float(meta.get("previousClose") or price)
        change = round(price - prev, 4)
        return json.dumps(
            {
                "ticker": params.ticker,
                "price": price,
                "previous_close": prev,
                "change": change,
                "change_percent": round((change / prev * 100), 2) if prev else 0,
                "currency": meta.get("currency", "USD"),
                "name": meta.get("shortName", params.ticker),
            }
        )
    except (TypeError, KeyError, ValueError) as exc:
        return json.dumps({"error": f"failed to parse response: {exc}"})


@mcp.tool
async def get_crypto_price(params: CryptoInput) -> str:
    """Get current cryptocurrency price from CoinGecko.

    Returns coin, price, change_24h_percent, market_cap, and currency.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=_HEADERS) as client:
            r = await client.get(_COINGECKO_URL.format(coin=params.coin, currency=params.currency))
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        return _handle_error(exc)

    try:
        if params.coin not in data:
            return json.dumps({"error": f"invalid coin '{params.coin}'"})
        c = data[params.coin]
        price = c.get(params.currency)
        if price is None:
            return json.dumps({"error": f"no price for currency '{params.currency}'"})
        change_24h = c.get(f"{params.currency}_24h_change")
        market_cap = c.get(f"{params.currency}_market_cap")
        return json.dumps(
            {
                "coin": params.coin,
                "price": float(price),
                "change_24h_percent": round(float(change_24h), 2) if change_24h is not None else 0,
                "market_cap": float(market_cap) if market_cap is not None else None,
                "currency": params.currency,
            }
        )
    except (TypeError, KeyError, ValueError) as exc:
        return json.dumps({"error": f"failed to parse response: {exc}"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Stock & crypto price MCP server (fastmcp)")
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
        print(f"Starting stock_price_mcp · HTTP transport · {args.host}:{args.port}/mcp")
        app = mcp.http_app()
        cors_app = CORSMiddleware(app=app, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
        uvicorn.run(cors_app, host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
