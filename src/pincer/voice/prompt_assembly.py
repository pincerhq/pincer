"""
The one place a live-call system prompt is assembled.

There used to be no such place. The prompt was built inside the voice channel,
and the briefing arrived along a path that differed depending on whether the
call came from the chat tool or from the dashboard API — so "the agent ignored
my purpose" could be true on one surface and false on the other, with nothing
in the code stating which was correct.

Every caller now goes through :func:`build_voice_system_prompt`, and a test
asserts that the dashboard path and the tool path produce byte-identical
prompts for identical input. Block ORDER is part of the contract, not an
accident of how the tuple was written:

1. persona — who the agent is;
2. **the binding task** — what this call is FOR, immediately after the
   persona so it outranks everything that follows;
3. language policy, tool rules, local time — how to behave;
4. thread context, receptionist rules, appointment slots — situational;
5. the phase instruction — what to do in this exact turn.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pincer.voice.engine import CallState
    from pincer.voice.state_machine import CallStateMachine

logger = logging.getLogger(__name__)

MAX_TASK_IN_PROMPT = 2000
MAX_INSTRUCTIONS_IN_PROMPT = 4000


def build_call_briefing_block(state: CallState, settings: Any, language: str, formality: str) -> str:
    """The binding task block for an outbound call; '' for inbound or untasked.

    Renders the user's task VERBATIM. We never paraphrase, summarize, or
    "improve" it: the whole point is that the agent is bound to what the user
    actually wrote.
    """
    from pincer.voice.engine import CallDirection

    if state.direction != CallDirection.OUTBOUND:
        return ""
    task = str(state.purpose or "").strip()[:MAX_TASK_IN_PROMPT]
    if not task:
        return ""

    from pincer.voice.prompts import get_prompt

    template = str(get_prompt("CALL_BRIEF", language, formality) or "")
    if not template:  # pragma: no cover — every language pack defines it
        return ""

    owner = str(getattr(settings, "voice_assistant_owner", "") or "").strip() or str(
        get_prompt("CALL_BRIEF_OWNER_DEFAULT", language, formality) or "your user"
    )
    target = str(state.target_name or "").strip() or str(state.target_number or "").strip()
    who_template = str(get_prompt("CALL_BRIEF_WHO", language, formality) or "")
    who = who_template.format(target=target, owner=owner) if target and who_template else ""

    instructions = str(state.instructions or "").strip()[:MAX_INSTRUCTIONS_IN_PROMPT]
    instructions_block = ""
    if instructions:
        instructions_block = str(get_prompt("CALL_BRIEF_INSTRUCTIONS", language, formality) or "").format(
            instructions=instructions
        )
    return template.format(task=task, who=who, instructions_block=instructions_block, owner=owner)


def build_time_context(settings: Any, language: str, formality: str) -> str:
    """Current local date/time + zone, so "tomorrow at noon" resolves locally."""
    try:
        from pincer.voice.localtime import get_voice_timezone, voice_now
        from pincer.voice.prompts import get_prompt
        from pincer.voice.tool_speech import spoken_datetime

        now = voice_now(settings)
        tz = str(get_voice_timezone(settings))
        template = str(get_prompt("TIME_CONTEXT", language, formality) or "")
        if not template:
            return ""
        stamp = f"{spoken_datetime(now, language)} ({now:%Y-%m-%d %H:%M})"
        return template.format(now=stamp, tz=tz)
    except Exception:
        logger.debug("time context unavailable", exc_info=True)
        return ""


def build_voice_system_prompt(
    state: CallState,
    settings: Any,
    sm: CallStateMachine,
    reception_block: str = "",
) -> str:
    """The per-turn system prompt for a live call.

    `state.language` is the single source of truth for which pack is used;
    `reception_block` is passed in rather than looked up because the
    receptionist session is owned by the channel and is per-call state, not
    something a prompt builder should reach for.
    """
    from pincer.voice.language import de_formality
    from pincer.voice.prompts import get_prompt
    from pincer.voice.scheduling import build_call_context

    language = state.language
    formality = de_formality(settings)
    parts = (
        # 1. Who the agent is.
        str(get_prompt("VOICE_SYSTEM_PROMPT", language, formality) or ""),
        # 2. What this call is FOR. Directly after the persona: a task placed
        #    below the conversation rules reads as background, and that is
        #    exactly how it used to get ignored.
        build_call_briefing_block(state, settings, language, formality),
        # 3. How to behave.
        str(get_prompt("LANGUAGE_POLICY", language, formality) or ""),
        str(get_prompt("IN_CALL_TOOL_RULES", language, formality) or ""),
        build_time_context(settings, language, formality),
        # 4. Situational context.
        str(state.metadata.get("thread_context") or ""),
        reception_block,
        build_call_context(state.call_sid, settings, language),
        # 5. This turn.
        sm.get_phase_instruction(language),
    )
    return "\n\n".join(p for p in parts if p)


__all__ = [
    "build_call_briefing_block",
    "build_time_context",
    "build_voice_system_prompt",
]
