"""``set_preferred_language`` — remember the language the user asked for.

A user who says "speak Ukrainian from now on" means every later reply, in
this conversation and the next ones. The model switches at once; this tool
writes the choice into the user's profile memory, which the agent puts into
every system prompt (see ``Agent._build_system_prompt``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pincer.memory.profile import clean_language, set_preferred_language

if TYPE_CHECKING:
    from pincer.memory.base import BaseMemoryBackend
    from pincer.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

TOOL_NAME = "set_preferred_language"


def make_set_preferred_language(memory: BaseMemoryBackend) -> Any:
    async def set_preferred_language_tool(language: str, context: dict[str, Any] | None = None) -> str:
        user_id = str((context or {}).get("pincer_user_id") or "")
        if not user_id:
            return "Error: no user is known for this conversation."
        value = clean_language(language)
        if value is None:
            return "Error: give the language as an ISO 639-1 code, e.g. 'uk' or 'de'."
        try:
            await set_preferred_language(memory, user_id, value)
        except Exception as e:
            logger.exception("set_preferred_language failed for %s", user_id)
            return f"Error: could not store the preferred language: {e}"
        return f"Preferred language set to {value}. Keep replying in it until the user asks for another."

    return set_preferred_language_tool


def register_language_tools(registry: ToolRegistry, memory: BaseMemoryBackend) -> None:
    registry.register(
        name=TOOL_NAME,
        description=(
            "Remember the language the user wants you to reply in, across this and future conversations. "
            "Call it when the user explicitly asks you to use a language from now on (e.g. 'speak Ukrainian', "
            "'розмовляй українською', 'switch back to English'), then reply in that language."
        ),
        handler=make_set_preferred_language(memory),
        parameters={
            "type": "object",
            "properties": {
                "language": {
                    "type": "string",
                    "description": "ISO 639-1 code of the language, e.g. 'uk', 'en', 'de'",
                },
            },
            "required": ["language"],
        },
    )


__all__ = ["TOOL_NAME", "make_set_preferred_language", "register_language_tools"]
