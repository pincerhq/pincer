"""
Voice-mode tool adapter — bridges the voice state machine with the tool registry.

Controls which tools are available during voice calls, plays filler phrases
while tools execute, and verbalizes tool results for TTS output.
"""

from __future__ import annotations

import json
import logging
import random
from typing import TYPE_CHECKING, Any

from pincer.voice.prompts import FILLER_PHRASES

if TYPE_CHECKING:
    from pincer.voice.engine import VoiceEngine

logger = logging.getLogger(__name__)

VOICE_ALLOWED_TOOLS = {
    "calendar_today",
    "calendar_week",
    "calendar_create",
    "email_check",
    "email_read",
    "email_send",
    "email_search",
    "web_search",
    "make_phone_call",
    "send_file",
    "send_image",
}

VOICE_EXCLUDED_TOOLS = {
    "shell_exec",
    "file_write",
    "file_read",
    "file_list",
    "python_exec",
    "browse",
    "screenshot",
}


def is_voice_compatible(tool_name: str) -> bool:
    """Check if a tool is usable during a voice call."""
    if tool_name in VOICE_EXCLUDED_TOOLS:
        return False
    if tool_name in VOICE_ALLOWED_TOOLS:
        return True
    return "__" in tool_name


def filter_voice_tools(tool_schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter tool schemas to only include voice-compatible tools."""
    return [s for s in tool_schemas if is_voice_compatible(s.get("name", ""))]


def get_filler_phrase(custom_phrases: str = "") -> str:
    """Get a random filler phrase to play while a tool executes."""
    phrases = FILLER_PHRASES
    if custom_phrases:
        try:
            custom = json.loads(custom_phrases)
            if isinstance(custom, list) and custom:
                phrases = custom
        except (json.JSONDecodeError, TypeError):
            pass
    return random.choice(phrases)


def verbalize_tool_result(tool_name: str, result: str) -> str:
    """Convert a raw tool result into a speakable summary.

    The LLM usually handles this via the voice system prompt, but this
    provides a fallback for structured data that needs pre-processing.
    """
    if not result or result.startswith("Error"):
        return result

    try:
        data = json.loads(result)
        if isinstance(data, dict):
            if "error" in data:
                return f"Sorry, there was a problem: {data['error']}"
            if tool_name == "web_search" and "results" in data:
                results = data["results"]
                if results:
                    first = results[0]
                    return f"Here's what I found: {first.get('title', '')}. {first.get('snippet', '')}"
            return result
        if isinstance(data, list):
            return f"I found {len(data)} items. " + (
                f"The first one is: {json.dumps(data[0])}" if data else ""
            )
    except (json.JSONDecodeError, TypeError, IndexError):
        pass

    if len(result) > 500:
        return result[:500] + "... I'll summarize the rest."

    return result


async def play_filler_and_execute(
    engine: VoiceEngine,
    call_sid: str,
    tool_fn: Any,
    tool_args: dict[str, Any],
    custom_phrases: str = "",
) -> str:
    """Play a filler phrase, execute the tool, and return the result."""
    filler = get_filler_phrase(custom_phrases)
    await engine.send_speech(call_sid, filler)

    try:
        result = await tool_fn(**tool_args)
        return str(result)
    except Exception as e:
        logger.exception("Tool execution failed during voice call %s", call_sid)
        return f"Error: {e}"
