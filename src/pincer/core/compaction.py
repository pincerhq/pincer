"""
History compaction by prompt tokens (issue #357, owner ruling Q20).

After a turn whose last request's whole prompt (uncached input, cache writes
and cache reads, from the API usage) went over `at_tokens`, the oldest
messages are replaced by one summary message so the whole prompt comes down to
about `to_tokens`. Tool call/result pairs stay whole and the latest exchange is
kept verbatim. The summary is a SYSTEM-role history message at the head of the
session (never part of the system prompt, which stays byte-identical); the
providers send it as a message of its own. When summarizing fails, the same
messages are dropped instead.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pincer.llm.base import LLMMessage, MessageRole

if TYPE_CHECKING:
    from pincer.config import Settings
    from pincer.config.llm import ReasoningEffort, ThinkingMode
    from pincer.core.session import Session, SessionManager
    from pincer.llm.base import BaseLLMProvider
    from pincer.llm.cost_tracker import CostTracker
    from pincer.llm.router import LLMRouter
    from pincer.memory.base import BaseMemoryBackend

logger = logging.getLogger(__name__)

# Room for a model that thinks before it writes the summary (issue #345, Q17); also the
# tokens reserved for the summary when choosing how much to cut.
_SUMMARY_MAX_TOKENS = 2048
_SUMMARIZE_PROMPT = (
    "You are a conversation summarizer. Summarize the following conversation "
    "into a concise paragraph capturing the key topics, decisions, facts, and "
    "any important context. Preserve names, dates, and specific details. "
    "Respond with ONLY the summary, no preamble."
)
SUMMARY_PREFIX = "[Previous conversation summary]\n"
SUMMARY_THRESHOLD_DEPRECATED = (
    "PINCER_SUMMARY_THRESHOLD no longer has an effect: history is compacted by prompt tokens "
    "(PINCER_COMPACT_AT_TOKENS / PINCER_COMPACT_TO_TOKENS)"
)
# A cut of fewer messages is not worth a summary call (or re-summarizing a summary alone)
_MIN_CUT = 2


@dataclass(frozen=True)
class LastRequest:
    """What a turn's last LLM request sent: its whole prompt in tokens, how many of the
    session's messages it carried, and the characters of its system prompt and tools."""

    prompt_tokens: int
    sent_messages: int
    overhead_chars: int


def _chars(message: LLMMessage) -> int:
    calls = json.dumps([tc.to_dict() for tc in message.tool_calls]) if message.tool_calls else ""
    return len(message.context) + len(message.content) + len(calls)


def choose_cut(messages: list[LLMMessage], last: LastRequest, target_tokens: int) -> int:
    """How many of the oldest messages to replace so the next prompt is about `target_tokens`.

    Tokens per character are calibrated from the last request (its prompt tokens over the
    characters it sent). The cut never ends inside a tool result run and never reaches the
    last user message, so the latest exchange stays verbatim. 0 means nothing to cut.
    """
    sent = min(last.sent_messages, len(messages))
    sent_chars = last.overhead_chars + sum(_chars(m) for m in messages[:sent])
    per_char = last.prompt_tokens / max(1, sent_chars)
    estimate = last.prompt_tokens + per_char * sum(_chars(m) for m in messages[sent:]) + _SUMMARY_MAX_TOKENS
    floor = max((i for i, m in enumerate(messages) if m.role == MessageRole.USER), default=0)
    cut = 0
    while estimate > target_tokens and cut < floor:
        estimate -= per_char * _chars(messages[cut])
        cut += 1
    while cut < floor and messages[cut].role == MessageRole.TOOL_RESULT:
        cut += 1
    return cut if cut >= _MIN_CUT else 0


def _transcript(messages: list[LLMMessage]) -> str:
    """The messages as a readable transcript for the summary model."""
    lines: list[str] = []
    for msg in messages:
        if msg.content:
            lines.append(f"{msg.role.value.capitalize()}: {msg.content}")
    return "\n".join(lines)


class Compactor:
    """Summarizes the oldest messages of a session whose prompt grew over the limit."""

    def __init__(
        self,
        llm: BaseLLMProvider,
        model: str | None,
        costs: CostTracker,
        sessions: SessionManager,
        memory: BaseMemoryBackend | None,
        *,
        provider: str,
        is_free: bool,
        at_tokens: int,
        to_tokens: int,
        reasoning_effort: ReasoningEffort | None = None,
        thinking: ThinkingMode | None = None,
    ) -> None:
        self._llm = llm
        self._model = model
        self._costs = costs
        self._sessions = sessions
        self._memory = memory
        self._provider = provider
        self._is_free = is_free
        self._at_tokens = at_tokens
        self._to_tokens = to_tokens
        # The summary settings; None means the provider's global setting
        self._reasoning_effort = reasoning_effort
        self._thinking = thinking

    def due(self, last: LastRequest) -> bool:
        """True when the last request's whole prompt went over the limit."""
        return last.prompt_tokens > self._at_tokens

    async def compact(self, session: Session, last: LastRequest) -> bool:
        """Replace the oldest messages by a summary (or drop them when that fails).

        The summary call runs without the session's turn lock, so a turn arriving meanwhile
        runs on the full history; the swap then applies only if those messages are still
        the session's oldest. True if the history changed.
        """
        if not self.due(last):
            return False
        cut = choose_cut(session.messages, last, self._to_tokens)
        if not cut:
            return False
        prefix = list(session.messages[:cut])
        summary = await self._summarize(session, prefix)
        replacement = LLMMessage(role=MessageRole.SYSTEM, content=SUMMARY_PREFIX + summary) if summary else None
        applied = await self._sessions.compact(session, prefix, replacement)
        if not applied:
            logger.info("Session %s changed while it was being compacted; compaction discarded", session.session_id)
            return False
        if replacement is None:
            logger.warning("Session %s: summary failed, dropped its %d oldest messages", session.session_id, cut)
        else:
            logger.info("Session %s compacted: %d oldest messages summarized", session.session_id, cut)
            await self._remember(session, summary)
        return True

    async def _summarize(self, session: Session, messages: list[LLMMessage]) -> str:
        """The summary text, or "" when it cannot be had (budget spent, call failed)."""
        if await self._costs.budget_reached():  # the pre-call budget check every LLM call goes through
            logger.warning("Daily budget reached: session %s is compacted without a summary", session.session_id)
            return ""
        request = LLMMessage(role=MessageRole.USER, content=f"Summarize this conversation:\n\n{_transcript(messages)}")
        try:
            response = await self._llm.complete(
                messages=[request],
                model=self._model,
                max_tokens=_SUMMARY_MAX_TOKENS,
                temperature=0.3,
                system=_SUMMARIZE_PROMPT,
                reasoning_effort=self._reasoning_effort,
                thinking=self._thinking,
            )
        except Exception:
            logger.warning("Summarizing session %s failed", session.session_id, exc_info=True)
            return ""
        await self._costs.record(
            provider=response.provider or self._provider,
            model=response.request_model or response.model or self._model or "",
            usage=response.usage,
            session_id=session.session_id,
            is_free=self._is_free,
        )
        return response.content.strip()

    async def _remember(self, session: Session, summary: str) -> None:
        """Save the summary as a memory, only with memory on for the session."""
        if self._memory is None or session.is_memory_free():
            return
        try:
            await self._memory.store_memory(user_id=session.user_id, content=summary, category="conversation_summary")
        except Exception:
            logger.warning("Saving the summary of session %s as a memory failed", session.session_id, exc_info=True)


def build_compactor(
    settings: Settings,
    router: LLMRouter,
    costs: CostTracker,
    sessions: SessionManager,
    memory: BaseMemoryBackend | None,
) -> Compactor | None:
    """The compactor `pincer run`, `pincer chat` and the API agent use, on the summary model.

    None (and a warning) when the summary provider cannot be built: the message-count trim
    then still bounds the history.
    """
    from pincer.config.llm import call_effort, call_thinking
    from pincer.llm.router import summary_target

    if settings.summary_threshold is not None:
        logger.warning(SUMMARY_THRESHOLD_DEPRECATED)
    try:
        llm, model = router.get_summarizer()
    except ValueError as e:
        logger.warning("History compaction is off: %s", e)
        return None
    provider, _ = summary_target(settings)
    return Compactor(
        llm,
        model,
        costs,
        sessions,
        memory,
        provider=provider,
        is_free=router.is_free(provider),
        at_tokens=settings.compact_at_tokens,
        to_tokens=settings.compact_to_tokens,
        # Empty means the global setting (the provider reads None that way), `none` sends nothing
        reasoning_effort=call_effort(settings.summary_reasoning_effort),
        thinking=call_thinking(settings.summary_thinking),
    )
