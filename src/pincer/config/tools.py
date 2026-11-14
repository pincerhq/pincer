from __future__ import annotations

from pathlib import Path  # noqa: TC003 (pydantic resolves it at runtime)
from typing import Annotated, Self

from pydantic import BaseModel, Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import NoDecode

from pincer.config.llm import (  # noqa: TC001 (pydantic resolves them at runtime)
    ReasoningEffortOverride,
    ThinkingOverride,
)
from pincer.utils.csv_list import split_csv


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
    tool_selection_headroom: int = Field(
        default=27,
        ge=0,
        description=(
            "Room a conversation's tool list keeps for later turns: a fresh selection sends "
            "tool_selection_max - headroom tools, and each later turn appends the new tools it needs at the end "
            "(the cached prefix stays); only past tool_selection_max is the list selected afresh"
        ),
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
    # Summaries only (issue #345): translated per model, against the summary model, like
    # PINCER_REASONING_EFFORT / PINCER_THINKING, which an empty value inherits.
    summary_reasoning_effort: ReasoningEffortOverride = Field(
        default="",
        description="Effort for conversation summaries ('' = PINCER_REASONING_EFFORT, 'default' = send nothing)",
    )
    summary_thinking: ThinkingOverride = Field(
        default="",
        description="Thinking for conversation summaries: adaptive | disabled "
        "('' = PINCER_THINKING, 'default' = send nothing)",
    )
    # Deprecated: history is compacted by prompt tokens now (issue #357, Q20). Kept so a value
    # still set is warned about at startup rather than silently ignored.
    summary_threshold: int | None = Field(
        default=None, description="Deprecated, no effect: see compact_at_tokens / compact_to_tokens"
    )
    compact_at_tokens: int = Field(
        default=80_000,
        ge=1_000,
        description="Compact a session's history after a turn whose last request's whole prompt "
        "(uncached input, cache writes and reads) was over this many tokens",
    )
    compact_to_tokens: int = Field(
        default=50_000,
        ge=1_000,
        description="The whole prompt a compaction aims for: the oldest messages become a summary until it fits",
    )
    compact_hard_tokens: int = Field(
        default=160_000,
        ge=1_000,
        description="Emergency limit: after a turn whose last prompt was over this many tokens and whose "
        "compaction failed, the oldest whole turns are dropped until the prompt is about compact_to_tokens",
    )

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
        description=(
            "Google OAuth client JSON (raw or base64), used when google_credentials.json is absent: by the "
            "consent flow, and to fill in a PINCER_GOOGLE_TOKEN_JSON that lacks client_id/client_secret"
        ),
    )
    # Named for its env var: the `google_oauth_dir()` method resolves the directory actually used.
    google_oauth_dir_override: Path | None = Field(
        default=None,
        validation_alias="PINCER_GOOGLE_OAUTH_DIR",
        description=(
            "Absolute directory for google_credentials.json and the Google token files. Unset = data_dir "
            "(or, deprecated, ./data when it already holds them)"
        ),
    )

    @field_validator("google_oauth_dir_override", mode="after")
    @classmethod
    def _expand_google_oauth_dir(cls, value: Path | None) -> Path | None:
        """Expand ``~`` once, at load: an unknown ``~user`` is a config error naming the
        setting, not a RuntimeError from every token lookup, doctor run and CLI command."""
        if value is None:
            return None
        try:
            return value.expanduser()
        except RuntimeError as exc:
            raise ValueError(
                f"PINCER_GOOGLE_OAUTH_DIR={value}: cannot expand '{value.parts[0]}' ({exc}) — "
                "use an absolute path or an existing user's ~"
            ) from exc

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
            return split_csv(v)
        return v

    @field_validator("approval_policy", mode="before")
    @classmethod
    def check_approval_policy(cls, v: str) -> str:
        from pincer.tools.approval import APPROVAL_MODES

        if v not in APPROVAL_MODES:
            raise ValueError(f"approval_policy must be one of: {', '.join(sorted(APPROVAL_MODES))}")
        return v

    @model_validator(mode="after")
    def check_tool_selection_headroom(self) -> Self:
        if self.tool_selection_headroom >= self.tool_selection_max:
            raise ValueError(
                f"tool_selection_headroom ({self.tool_selection_headroom}) must be below "
                f"tool_selection_max ({self.tool_selection_max})"
            )
        return self

    @model_validator(mode="after")
    def check_compaction_target(self) -> Self:
        if self.compact_to_tokens >= self.compact_at_tokens:
            raise ValueError(
                f"compact_to_tokens ({self.compact_to_tokens}) must be below "
                f"compact_at_tokens ({self.compact_at_tokens})"
            )
        return self

    @model_validator(mode="after")
    def check_compaction_hard_limit(self) -> Self:
        if self.compact_hard_tokens <= self.compact_at_tokens:
            raise ValueError(
                f"compact_hard_tokens ({self.compact_hard_tokens}) must be above "
                f"compact_at_tokens ({self.compact_at_tokens})"
            )
        return self
