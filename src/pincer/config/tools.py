from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr, ValidationError, field_validator


class ToolSettings(BaseModel):
    # ── Shell ─────────────────────────────────────────────
    shell_enabled: bool = Field(default=True, description="Enable shell execution tool")
    shell_timeout: int = Field(default=30, ge=5, le=300, description="Shell command timeout secs")
    shell_require_approval: bool = Field(
        default=True,
        description="Require user approval before running shell commands",
    )

    # ── Memory ────────────────────────────────────────────
    memory_enabled: bool = Field(default=True, description="Enable memory system")
    memory_backend: str = Field(
        default="sqlite",
        description="Memory backend: 'sqlite' (local DB) or 'mcp' (external MCP server)",
    )
    memory_mcp_server: str = Field(
        default="sqlite_vec_memory",
        description=(
            "MCP server name used when memory_backend='mcp'. "
            "Connection to MCP server will taken from existing MCP servers pool"
        ),
    )
    summary_model: str = Field(
        default="claude-haiku-4-5-20251001",
        description="Cheap model for conversation summarization",
    )
    summary_threshold: int = Field(default=20, ge=5, description="Summarize conversation after N messages")

    # ── Image Generation ──────────────────────────────────
    image_provider: str = Field(
        default="auto",
        description="Image provider: auto | fal | gemini",
    )
    fal_key: SecretStr = Field(default=SecretStr(""), description="fal.ai API key")
    fal_model: str = Field(default="fal-ai/nano-banana-2", description="fal.ai image model")
    gemini_api_key: SecretStr = Field(default=SecretStr(""), description="Google Gemini API key")
    image_model_gemini: str = Field(default="gemini-2.5-flash-image", description="Gemini image generation model")
    image_max_cost_per_request: float = Field(
        default=0.10, ge=0.0, description="Max cost per image generation request in USD"
    )
    image_daily_limit: int = Field(default=50, ge=0, description="Max image generations per day (0 = unlimited)")

    # ── Scheduling ────────────────────────────────────────
    schedule_tool_enabled: bool = Field(default=True, description="Enable schedule_create/list/remove/toggle tools")
    max_schedules_per_user: int = Field(
        default=20, ge=1, description="Max active recurring schedules per user (cost/abuse guard)"
    )
    min_schedule_interval_minutes: int = Field(
        default=15,
        ge=1,
        description="Reject schedules that would fire more often than this (guards against e.g. '* * * * *')",
    )

    @field_validator("memory_backend", mode="before")
    @classmethod
    def check_value_allowed(cls, v: str) -> str:
        if v not in ["sqlite", "mcp"]:
            raise ValidationError("Provided value not in allowed list: sqlite, mcp")
        return v
