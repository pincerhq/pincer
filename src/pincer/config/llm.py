from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import NoDecode


class LLMProvider(StrEnum):
    """The two well-known provider names. Any other name is resolved as a
    configured OpenAI-/Anthropic-compatible endpoint (see `pincer.llm.router`)."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class LLMSettings(BaseModel):
    # Provider names are free-form strings. "openai"/"anthropic" are well-known
    # (key only); any other name must match a configured *_compatible_provider.
    default_provider: str = Field(default="anthropic", description="Primary LLM provider name")
    # NoDecode: skip pydantic-settings' JSON decoding of the env value so the
    # comma-splitting validator below receives the raw string.
    fallback_providers: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="Up to 3 failover provider names (comma-separated)",
    )
    summary_provider: str | None = Field(
        default=None,
        description="Provider used by the Summarizer; defaults to the primary",
    )

    # Well-known providers — set the key, base URL is built in.
    # Each carries an optional model so failover targets the right model id
    # (empty → falls back to default_model).
    anthropic_api_key: SecretStr = Field(default=SecretStr(""), description="Anthropic API key")
    anthropic_model: str = Field(
        default="", description="Model for the 'anthropic' provider (defaults to default_model)"
    )
    openai_api_key: SecretStr = Field(default=SecretStr(""), description="OpenAI API key")
    openai_base_url: str = Field(default="https://api.openai.com/v1", description="OpenAI API base URL")
    openai_model: str = Field(default="", description="Model for the 'openai' provider (defaults to default_model)")

    # Compatible endpoints — name each one, set its base URL; key optional; model
    # falls back to default_model when empty.
    openai_compatible_provider: str = Field(
        default="", description="Name for the OpenAI-wire compatible endpoint (e.g. grok, ollama)"
    )
    openai_compatible_base_url: str = Field(default="", description="OpenAI-wire compatible endpoint base URL")
    openai_compatible_api_key: SecretStr = Field(
        default=SecretStr(""), description="OpenAI-wire compatible endpoint API key (optional)"
    )
    openai_compatible_model: str = Field(
        default="", description="Model for the OpenAI-wire compatible endpoint (defaults to default_model)"
    )
    anthropic_compatible_provider: str = Field(
        default="", description="Name for the Anthropic-wire compatible endpoint"
    )
    anthropic_compatible_base_url: str = Field(default="", description="Anthropic-wire compatible endpoint base URL")
    anthropic_compatible_api_key: SecretStr = Field(
        default=SecretStr(""), description="Anthropic-wire compatible endpoint API key (optional)"
    )
    anthropic_compatible_model: str = Field(
        default="", description="Model for the Anthropic-wire compatible endpoint (defaults to default_model)"
    )

    @field_validator("fallback_providers", mode="before")
    @classmethod
    def parse_fallback_providers(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            if not v.strip():
                return []
            return [name.strip() for name in v.split(",") if name.strip()]
        return v

    default_model: str = Field(
        default="claude-sonnet-4-5-20250929",
        description="Default model identifier",
    )
    prompt_cache_tools: bool = Field(
        default=True,
        description="Anthropic prompt caching on the (static) tool schemas — the largest prompt prefix. "
        "Major TTFT/cost win on multi-turn conversations, especially voice (Sprint 5 T5.4).",
    )
    max_tokens: int = Field(default=8192, ge=1, le=128000)
    temperature: float = Field(default=0.5, ge=0.0, le=2.0)

    agent_name: str = Field(default="Pincer", description="Agent display name")
    system_prompt: str = Field(
        default=(
            "You are Pincer, a helpful personal AI assistant. "
            "You are concise, friendly, and proactive. "
            "You have access to tools and use them when they help answer the user's question. "
            "When uncertain, say so honestly. "
            "Always respond in the same language the user writes in.\n\n"
            "IMPORTANT: When you have image or GIF URLs, you MUST use the send_image tool "
            "to display them visually in the chat. NEVER paste image/GIF URLs as plain text. "
            "Call send_image for each image URL so the user sees the actual picture inline.\n\n"
            "When you create a calendar event, your response MUST include the direct link to the event "
            "(from the tool result). If the tool returns an error, tell the user exactly what went wrong."
        ),
        description="System prompt (the agent's personality / soul)",
    )
    max_tool_iterations: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Max ReAct loop iterations before forcing a response",
    )
    max_session_messages: int = Field(
        default=50,
        ge=10,
        le=500,
        description="Max messages in session before trimming",
    )
