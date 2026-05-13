from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, SecretStr


class LLMProvider(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GROK = "grok"


class LLMSettings(BaseModel):
    default_provider: LLMProvider = LLMProvider.ANTHROPIC
    anthropic_api_key: SecretStr = Field(default=SecretStr(""), description="Anthropic API key")
    openai_api_key: SecretStr = Field(default=SecretStr(""), description="OpenAI API key")
    grok_api_key: SecretStr = Field(default=SecretStr(""), description="xAI Grok API key")

    default_model: str = Field(
        default="claude-sonnet-4-5-20250929",
        description="Default model identifier",
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
