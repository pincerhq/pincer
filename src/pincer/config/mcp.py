"""API keys Pincer's own built-in tools use directly (and happen to also be
handed to matching MCP servers, e.g. newsapi/openweathermap).

Credentials that only an external MCP server needs (Microsoft 365 client
id/secret/tenant, etc.) do NOT belong here — Pincer core has no reason to
know their names. Those flow straight from the process environment (see
`${VAR}` interpolation in pincer.toml's [[mcp.servers]] `env` blocks, resolved
against os.environ by pincer/mcp/config.py, which loads `.env` itself).
This part of config has been deprecated and left only for old skills system compatibility
"""

from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr


class MCPSettings(BaseModel):
    newsapi_key: SecretStr = Field(default=SecretStr(""), description="NewsAPI key for the newsapi MCP server")
    openweathermap_api_key: SecretStr = Field(
        default=SecretStr(""), description="OpenWeatherMap key for the openweathermap MCP server"
    )
