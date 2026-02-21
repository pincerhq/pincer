"""
Auto-summarizer for long conversations.

When a conversation exceeds a configurable message threshold, older messages
are summarized using a cheap LLM model and the summary is stored as a
searchable memory entry. The original messages are replaced with a single
system message containing the summary.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pincer.llm.base import LLMMessage, LLMResponse, MessageRole

if TYPE_CHECKING:
    from pincer.core.session import Session, SessionManager
    from pincer.llm.base import BaseLLMProvider
    from pincer.memory.store import MemoryStore

logger = logging.getLogger(__name__)

_SUMMARIZE_PROMPT = (
    "You are a conversation summarizer. Summarize the following conversation "
    "into a concise paragraph capturing the key topics, decisions, facts, and "
    "any important context. Preserve names, dates, and specific details. "
    "Respond with ONLY the summary, no preamble."
)


class Summarizer:
    """Summarizes long conversations and stores summaries as memories."""

    def __init__(
        self,
        llm: BaseLLMProvider,
        memory_store: MemoryStore,
        session_manager: SessionManager,
        summary_model: str = "claude-haiku-4-5-20251001",
        threshold: int = 20,
    ) -> None:
        self._llm = llm
        self._memory = memory_store
        self._sessions = session_manager
        self._summary_model = summary_model
        self._threshold = threshold

    async def maybe_summarize(self, session: Session) -> bool:
        """
        Check if session needs summarization and do it if so.
        Returns True if summarization occurred.
        """
        non_system = [m for m in session.messages if m.role != MessageRole.SYSTEM]
        if len(non_system) < self._threshold:
            return False

        # Take the older half of non-system messages for summarization
        split_point = len(non_system) // 2
        # Never split in the middle of a tool_use/tool_result pair
        while split_point < len(non_system) and non_system[split_point].role == MessageRole.TOOL_RESULT:
            split_point += 1
        to_summarize = non_system[:split_point]
        to_keep = non_system[split_point:]

        if not to_summarize:
            return False

        logger.info(
            "Summarizing %d messages for session %s",
            len(to_summarize), session.session_id,
        )

        summary_text = await self._generate_summary(to_summarize)
        if not summary_text:
            return False

        # Store as a searchable memory
        await self._memory.store_memory(
            user_id=session.user_id,
            content=summary_text,
            category="conversation_summary",
        )

        # Rebuild session: system messages + summary + remaining messages
        system_msgs = [m for m in session.messages if m.role == MessageRole.SYSTEM]
        summary_msg = LLMMessage(
            role=MessageRole.SYSTEM,
            content=f"[Previous conversation summary]\n{summary_text}",
        )

        session.messages = system_msgs + [summary_msg] + to_keep
        await self._sessions._persist(session)  # noqa: SLF001

        logger.info(
            "Summarized session %s: %d -> %d messages",
            session.session_id, len(to_summarize) + len(to_keep) + len(system_msgs),
            len(session.messages),
        )
        return True

    async def _generate_summary(self, messages: list[LLMMessage]) -> str:
        """Call the LLM to generate a conversation summary."""
        conversation_text = self._format_messages(messages)

        summary_request = LLMMessage(
            role=MessageRole.USER,
            content=f"Summarize this conversation:\n\n{conversation_text}",
        )

        try:
            response: LLMResponse = await self._llm.complete(
                messages=[summary_request],
                model=self._summary_model,
                max_tokens=500,
                temperature=0.3,
                system=_SUMMARIZE_PROMPT,
            )
            return response.content.strip()
        except Exception:
            logger.exception("Failed to generate summary")
            return ""

    @staticmethod
    def _format_messages(messages: list[LLMMessage]) -> str:
        """Format messages into a readable conversation transcript."""
        lines: list[str] = []
        for msg in messages:
            role = msg.role.value.capitalize()
            if msg.content:
                lines.append(f"{role}: {msg.content}")
        return "\n".join(lines)
