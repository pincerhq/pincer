from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, cast

from pydantic import BaseModel, BeforeValidator, Field, SecretStr, field_validator
from pydantic_settings import NoDecode

from pincer.utils.csv_list import split_csv

# "" = not set: nothing is sent and the model runs on its own default.
ReasoningEffort = Literal["", "low", "medium", "high", "xhigh", "max"]
ThinkingMode = Literal["", "adaptive", "disabled"]


# The voice and summary overrides. "" inherits the global setting; "default" sends nothing, so the
# model runs on its own default even when the global setting is set. "none" is refused: on OpenAI
# it is itself a reasoning level (no reasoning), so it would be read as "off" and mean the opposite.
NO_SETTING = "default"
_NONE_REJECTED = (
    f"'none' is not accepted: use '{NO_SETTING}' to send nothing (the model's own default), "
    "or 'disabled' to turn thinking off."
)


def _reject_none(value: object) -> object:
    if isinstance(value, str) and value.strip().lower() == "none":
        raise ValueError(_NONE_REJECTED)
    return value


ReasoningEffortOverride = Annotated[
    Literal["", "default", "low", "medium", "high", "xhigh", "max"], BeforeValidator(_reject_none)
]
ThinkingOverride = Annotated[Literal["", "default", "adaptive", "disabled"], BeforeValidator(_reject_none)]


def call_effort(override: ReasoningEffortOverride) -> ReasoningEffort | None:
    """The `reasoning_effort` argument of one call: None = the provider's global setting,
    "" = send nothing."""
    if override == "":
        return None
    return "" if override == NO_SETTING else cast("ReasoningEffort", override)


def call_thinking(override: ThinkingOverride) -> ThinkingMode | None:
    """The `thinking` argument of one call, as `call_effort`."""
    if override == "":
        return None
    return "" if override == NO_SETTING else cast("ThinkingMode", override)


def effective_effort(override: ReasoningEffortOverride, global_: ReasoningEffort) -> ReasoningEffort:
    """The effort a call carries: what `call_effort` says, with the global setting for None."""
    value = call_effort(override)
    return global_ if value is None else value


def effective_thinking(override: ThinkingOverride, global_: ThinkingMode) -> ThinkingMode:
    """The thinking a call carries, as `effective_effort`."""
    value = call_thinking(override)
    return global_ if value is None else value


# Internal JSON-extraction calls (call outcome, thread summary merge, onboarding answer) pin
# these whatever the global settings are: their output limit is small and fixed, so a model
# that thinks past it would cut the JSON off. The resolver drops what a model can't take.
STRUCTURED_CALL_EFFORT: ReasoningEffort = "low"
STRUCTURED_CALL_THINKING: ThinkingMode = "disabled"


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
        description=(
            "Provider that writes history compaction summaries; defaults to the primary. Built on demand from its "
            "credentials when it is not in the failover pool"
        ),
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
    prompt_cache_key_providers: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["openai", "azure", "azure-openai"],
        description="OpenAI-wire providers sent a prompt_cache_key (comma-separated names); other endpoints may "
        "reject the unknown field. A keyless endpoint never gets one",
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

    server_compaction_providers: Annotated[list[str] | None, NoDecode] = Field(
        default=None,
        description="Anthropic-wire providers that take Anthropic's beta features (thinking-binding controls "
        "now, server-side compaction later), comma-separated names. Unset = 'anthropic' and the configured "
        "Anthropic-compatible name; 'none' (or an empty list in pincer.toml) = no endpoint, for a proxy that "
        "rejects the betas (issue #358)",
    )
    strict_thinking_binding: bool = Field(
        default=False,
        description="Test instrument: send prefix_mismatch_behavior 'error' instead of 'drop_block', so a thinking "
        "block that fails Anthropic's prefix check fails the request instead of being dropped (issue #358)",
    )

    @field_validator("fallback_providers", "prompt_cache_key_providers", mode="before")
    @classmethod
    def parse_fallback_providers(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return split_csv(v)
        return v

    @field_validator("server_compaction_providers", mode="before")
    @classmethod
    def parse_server_compaction_providers(cls, v: str | list[str] | None) -> list[str] | None:
        """Comma-separated names. An empty environment variable is ignored by the settings loader,
        so "none" is how the environment turns the list off."""
        if isinstance(v, str):
            names = split_csv(v)
            return [] if [n.lower() for n in names] == ["none"] else names
        return v

    default_model: str = Field(
        default="claude-haiku-4-5-20251001",
        description="Default model identifier",
    )
    prompt_cache_tools: bool = Field(
        default=True,
        description="Anthropic prompt caching on requests with tools: the tools and the system prompt for "
        "1 hour, the conversation automatically for 5 minutes (issue #357). False sends no cache breakpoint.",
    )
    max_tokens: int = Field(default=8192, ge=1, le=128000)
    temperature: float = Field(default=0.5, ge=0.0, le=2.0)
    reasoning_effort: ReasoningEffort = Field(
        default="",
        description="Effort for every request ('' = the model's default). Translated per model: stepped down "
        "where a level is unsupported, dropped (warned once) where the model takes no effort",
    )
    thinking: ThinkingMode = Field(
        default="",
        description="'adaptive' turns thinking on, 'disabled' off, where the model allows it ('' = the model's "
        "default). Thinking counts toward max_tokens",
    )

    agent_name: str = Field(default="Pincer", description="Agent display name")
    system_prompt: str = Field(
        default=(
            "You are Pincer, a helpful personal AI assistant. "
            "You are concise, friendly, and proactive. "
            "You have access to tools and use them when they help answer the user's question. "
            "When uncertain, say so honestly. "
            "Language: by default, respond in the language the user writes in. If the user explicitly "
            'asks for a language ("speak Ukrainian", "розмовляй українською"), switch to it at once '
            "and keep using it in every reply until they ask for another, even when they keep writing "
            "in a different language. Never refuse a language you can write.\n\n"
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
        description="Past this many messages a session is cut to the newest 3/5 of them (50 -> 30)",
    )
