from __future__ import annotations

from pydantic import AnyHttpUrl, BaseModel, Field, SecretStr


class APISettings(BaseModel):
    # ── Dashboard ─────────────────────────────────────────
    dashboard_token: SecretStr = Field(default=SecretStr(""), description="Bearer token for API auth")
    dashboard_host: str = Field(default="127.0.0.1", description="API server bind host")
    dashboard_port: int = Field(default=8080, ge=1, le=65535, description="API server port")
    dashboard_url: str = Field(default="", description="Dashboard CORS origin URL")
    dashboard_dist: str = Field(default="", description="Dashboard dist path override (Docker)")
    web_chat_token: SecretStr = Field(default=SecretStr(""), description="Bearer token for web chat API auth")
    web_chat_url: str = Field(default="", description="Web chat CORS origin URL")

    # ── Webhooks ──────────────────────────────────────────
    webhook_port: int = Field(default=8765, description="Webhook listener port")
    webhook_secret: SecretStr = Field(default=SecretStr(""), description="Webhook HMAC secret")

    # ── Telemetry ─────────────────────────────────────────
    telemetry_dsn: AnyHttpUrl | None = Field(
        None,
        description="OpenTelemetry DSN url with token sent as uptrace-dsn header (None = telemetry disabled)",
    )

    # ── Security ──────────────────────────────────────────
    audit_disabled: bool = Field(default=False, description="Disable audit logging")
    rate_messages_per_min: int = Field(default=30, ge=1, description="Per-user message rate limit")
    rate_tools_per_min: int = Field(default=20, ge=1, description="Per-user tool call rate limit")
    max_concurrent_llm: int = Field(default=5, ge=1, description="Max concurrent LLM requests")
    debug: bool = Field(default=False, description="Enable debug mode")
    tool_approval: str = Field(
        default="auto",
        description="Tool approval mode: auto | manual | allowlist",
    )
    skill_sandbox_disabled: bool = Field(default=False, description="Disable skill sandbox")
