from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import NoDecode


class ToolSettings(BaseModel):
    # ── Shell ─────────────────────────────────────────────
    shell_enabled: bool = Field(default=True, description="Enable shell execution tool")
    shell_timeout: int = Field(default=30, ge=5, le=300, description="Shell command timeout secs")
    shell_require_approval: bool = Field(
        default=True,
        description="Require user approval before running shell commands",
    )

    # ── Approval policy ───────────────────────────────────
    approval_policy: str = Field(
        default="critical",
        description=(
            "Which write-capable tools prompt for in-chat approval: "
            "'critical' (only irreversible/outward-facing/executing tools), "
            "'all' (every tool that declares require_approval), "
            "'none' (never prompt — trusted non-interactive deployments only)"
        ),
    )
    approval_always: str = Field(
        default="",
        description="Comma-separated tool names that always require approval, whatever the policy says",
    )
    approval_never: str = Field(
        default="",
        description="Comma-separated tool names that never require approval, whatever the policy says",
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
    # ── Tool selection (tool RAG) ─────────────────────────
    tool_selection_enabled: bool = Field(
        default=True,
        description=(
            "With an OpenAI-wire default or failover provider and more than tool_selection_max tools, send "
            "each chat turn only: the tool_selection_default tools, every tool of memory_mcp_server, tools "
            "used earlier in the conversation, and the best semantic matches for the request (ranked on "
            "memory_mcp_server). Anthropic-only setups always get every tool"
        ),
    )
    tool_selection_max: int = Field(
        default=127,
        ge=1,
        le=127,
        description="Most tools sent per chat turn (OpenAI rejects more than 128; 127 keeps a margin)",
    )
    tool_selection_timeout: float = Field(
        default=3.0,
        gt=0,
        le=30,
        description="Seconds a chat turn waits for tool ranking before sending tools in registry order",
    )
    tool_selection_default: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "make_phone_call",
            "get_call_transcript",
            "send_file",
            "send_image",
            "schedule_create",
            "schedule_list",
            "schedule_remove",
            "schedule_toggle",
            "load_skill",
            "load_skill_reference",
            "run_skill_script",
        ],
        description=(
            "Tools always sent when selection applies: names or fnmatch patterns (comma-separated). "
            "Setting it replaces the whole default list"
        ),
    )
    summary_model: str = Field(
        default="claude-haiku-4-5-20251001",
        description=(
            "Cheap model for conversation summarization. 'provider:model' (e.g. 'anthropic:claude-haiku-4-5') "
            "also picks the provider, overriding summary_provider. A claude-* id on an OpenAI-wire provider "
            "is ignored in favour of that provider's own model"
        ),
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

    # ── Google OAuth (alternative to token files in the data volume) ──
    google_token_json: SecretStr = Field(
        default=SecretStr(""),
        description=(
            "Google OAuth token JSON (raw or base64) — overrides google_workspace_token.json / "
            "google_token.json. Generate with `pincer google setup --print-env` or `pincer google export-token`."
        ),
    )
    google_credentials_json: SecretStr = Field(
        default=SecretStr(""),
        description="Google OAuth client JSON (raw or base64), used by the consent flow when the file is absent",
    )

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

    @field_validator("tool_selection_default", mode="before")
    @classmethod
    def parse_tool_selection_default(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [name.strip() for name in v.split(",") if name.strip()]
        return v

    @field_validator("approval_policy", mode="before")
    @classmethod
    def check_approval_policy(cls, v: str) -> str:
        from pincer.tools.approval import APPROVAL_MODES

        if v not in APPROVAL_MODES:
            raise ValueError(f"approval_policy must be one of: {', '.join(sorted(APPROVAL_MODES))}")
        return v
