"""MCP server credentials — API keys and credentials for standalone MCP servers."""

from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr


class MCPSettings(BaseModel):
    # ── Microsoft 365 MCP server ──────────────────────────────────
    ms365_client_id: str = Field(default="", description="Azure App (client) ID")
    ms365_client_secret: str = Field(default="", description="Client secret (not needed for device code flow)")
    ms365_tenant_id: str = Field(default="consumers", description="'consumers', 'common', or org tenant GUID")

    # ── External API keys (used by MCP servers) ───────────────────
    newsapi_key: SecretStr = Field(default=SecretStr(""), description="NewsAPI key for the newsapi MCP server")
    openweathermap_api_key: SecretStr = Field(
        default=SecretStr(""), description="OpenWeatherMap key for the openweathermap MCP server"
    )
