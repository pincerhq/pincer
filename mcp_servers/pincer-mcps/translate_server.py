"""
translate_mcp — LibreTranslate MCP server built with fastmcp.

Transports:
    stdio (default):  python server.py --transport stdio
    HTTP:             python server.py --transport http [--host 0.0.0.0] [--port 8000]
        MCP endpoint → http://localhost:8000/mcp

    Or use the fastmcp CLI:
        fastmcp run server.py --transport http --port 8000

Environment:
    LIBRETRANSLATE_URL   Base URL of the LibreTranslate instance
                         (default: https://libretranslate.com)
    API_KEY              API key if the instance requires one (optional)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from typing import Optional

import httpx
import uvicorn
from fastapi.responses import JSONResponse
from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
LIBRETRANSLATE_URL = os.environ.get("LIBRETRANSLATE_URL", "https://libretranslate.com").rstrip("/")
API_KEY = os.environ.get("API_KEY", "")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="translate_mcp",
    instructions=(
        "LibreTranslate MCP server. "
        "Use translate_text to translate text between languages, "
        "and list_languages to discover supported language codes."
    ),
)


def _handle_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            msg = exc.response.json().get("error", "")
        except Exception:
            msg = ""
        return f"API error {exc.response.status_code}: {msg}".strip(": ")
    if isinstance(exc, httpx.TimeoutException):
        return "Error: request timed out – please try again"
    return f"Error: {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


class TranslateInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    text: str = Field(description="Text to translate")
    target: str = Field(description="Target language code, e.g. 'en', 'es', 'fr'")
    source: str = Field(
        default="auto",
        description="Source language code, or 'auto' to detect automatically (default)",
    )

    @field_validator("text", "target")
    @classmethod
    def _not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("value cannot be empty")
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
            "service": "translate-mcp",
            "version": "0.1.0",
            "transport": "http",
            "backend": LIBRETRANSLATE_URL,
        }
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool
async def translate_text(params: TranslateInput) -> str:
    """Translate text via LibreTranslate.

    Returns translated_text, source_language (detected when source='auto'), and target_language.
    """
    payload: dict = {"q": params.text, "source": params.source, "target": params.target}
    if API_KEY:
        payload["api_key"] = API_KEY

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(f"{LIBRETRANSLATE_URL}/translate", json=payload)
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        return _handle_error(exc)

    translated = data.get("translatedText", "")
    detected = data.get("detectedLanguage", {}).get("language") if params.source == "auto" else params.source
    return json.dumps(
        {
            "translated_text": translated,
            "source_language": detected or params.source,
            "target_language": params.target,
        }
    )


@mcp.tool
async def list_languages() -> str:
    """List all language codes supported by the LibreTranslate instance.

    Returns a list of objects with 'code' and 'name' fields.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{LIBRETRANSLATE_URL}/languages")
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        return _handle_error(exc)

    languages = [
        {"code": item.get("code", ""), "name": item.get("name", "")}
        for item in data
        if isinstance(item, dict)
    ]
    return json.dumps({"languages": languages})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LibreTranslate MCP server (fastmcp)")
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
        print(f"Starting translate_mcp · HTTP transport · {args.host}:{args.port}/mcp")
        app = mcp.http_app()
        app = CORSMiddleware(app=app, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
        uvicorn.run(app, host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")
