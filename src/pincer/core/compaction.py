"""
History compaction by prompt tokens (issue #357, owner rulings Q20 and Q22; issue #358 slice 4).

Who writes the summary depends on the endpoint that served the turn's last request (`pick_strategy`):
an Anthropic-wire endpoint in ``server_compaction_providers`` summarizes on its own server, on the
conversation's own model (`Compactor._compact_on_server`); every other endpoint uses Pincer's summary
model, described next.

After a turn whose last request's whole prompt (uncached input, cache writes
and cache reads, from the API usage) went over `at_tokens`, the oldest
messages are replaced by one summary message so the whole prompt comes down to
about `to_tokens`. Tool call/result pairs stay whole and the latest exchange is
kept verbatim. The summary is a SYSTEM-role history message at the head of the
session (never part of the system prompt, which stays byte-identical); the
providers send it as a message of its own. When no summary can be had (the call
fails, or the daily budget is reached), the history is kept as it is: compaction is
tried again after a later turn, and the message-count trim still bounds the session.

Server compaction summarizes exactly the messages of the last request (same model, system prompt and
tools, so the prompt is a cache read and the thinking of the turns kept after the block stays valid).
The returned block replaces them as one SYSTEM-role message at the head, tagged with its wire; whatever
arrived meanwhile stays after it. Nothing changes when no summary comes back.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

from pincer.llm.anthropic_common import AnthropicCompatibleProvider
from pincer.llm.base import LLMMessage, MessageRole
from pincer.llm.capabilities import endpoint_capabilities

if TYPE_CHECKING:
    from collections.abc import Callable

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
# The stop reason of a Messages response that holds a compaction block (and nothing else)
STOP_REASON_COMPACTION = "compaction"
# A cut of fewer messages is not worth a summary call (or re-summarizing a summary alone)
_MIN_CUT = 2


def _chars(message: LLMMessage) -> int:
    """The characters a request carries for `message`: its text, tool calls and the thinking it keeps."""
    calls = json.dumps([tc.to_dict() for tc in message.tool_calls]) if message.tool_calls else ""
    thinking = sum(len(block.get("thinking", "")) + len(block.get("data", "")) for block in message.raw)
    return len(message.context) + len(message.content) + len(calls) + thinking


@dataclass(frozen=True)
class SentRequest:
    """How a turn's last LLM request was made, for a server compaction to repeat it: the
    provider that served it, the model it asked for, its system prompt and tools, and the
    per-call reasoning settings (voice sets its own)."""

    provider: str
    model: str
    system: str | None
    tools: list[dict[str, Any]] | None
    reasoning_effort: ReasoningEffort | None = None
    thinking: ThinkingMode | None = None


@dataclass(frozen=True)
class LastRequest:
    """What a turn's last LLM request sent: its whole prompt in tokens, the tokens per
    character of what it carried, and the characters of its system prompt and tools.
    `sent` (the very message objects) and `request` are what a server compaction summarizes
    and how it asks."""

    prompt_tokens: int
    tokens_per_char: float
    overhead_chars: int
    sent: tuple[LLMMessage, ...] = field(default=(), compare=False, repr=False)
    request: SentRequest | None = field(default=None, compare=False, repr=False)

    @classmethod
    def measure(
        cls, prompt_tokens: int, sent: list[LLMMessage], overhead_chars: int, *, request: SentRequest | None = None
    ) -> LastRequest:
        """Calibrate on the messages the request sent, as they were sent: end_turn later trims
        the session, so they cannot be counted afterwards (PR #366).

        Images carry prompt tokens but no characters, so a request that sent one gets a higher
        rate and may summarize a little more. Photos now stay in the history, so every later
        request of the conversation carries them too (issue #358).
        """
        sent_chars = overhead_chars + sum(_chars(m) for m in sent)
        return cls(prompt_tokens, prompt_tokens / max(1, sent_chars), overhead_chars, tuple(sent), request)


def choose_cut(messages: list[LLMMessage], last: LastRequest, target_tokens: int) -> int:
    """How many of the oldest messages to replace so the next prompt is about `target_tokens`.

    The next prompt is estimated as the last request's tokens per character times the
    history as it is now (after the turn's trim) plus the system prompt and tools. The cut
    never ends inside a tool result run and never reaches the last user message, so the
    latest exchange stays verbatim. 0 means nothing to cut.
    """
    per_char = last.tokens_per_char
    estimate = per_char * (last.overhead_chars + sum(_chars(m) for m in messages)) + _SUMMARY_MAX_TOKENS
    floor = max((i for i, m in enumerate(messages) if m.role == MessageRole.USER), default=0)
    cut = 0
    while estimate > target_tokens and cut < floor:
        estimate -= per_char * _chars(messages[cut])
        cut += 1
    while cut < floor and messages[cut].role == MessageRole.TOOL_RESULT:
        cut += 1
    return cut if cut >= _MIN_CUT else 0


def _follows_a_summary(kept: list[LLMMessage]) -> bool:
    """True when `kept` can follow a server compaction block: it is empty, or it starts with an
    assistant message. The API merges a first kept message of the summarized messages' last role
    (a user message, or the tool result that is one) into them, which would lose it."""
    return not kept or kept[0].role == MessageRole.ASSISTANT


def _transcript(messages: list[LLMMessage]) -> str:
    """The messages as a readable transcript for the summary model."""
    lines: list[str] = []
    for msg in messages:
        if msg.content:
            lines.append(f"{msg.role.value.capitalize()}: {msg.content}")
    return "\n".join(lines)


class CompactionStrategy(StrEnum):
    """Who writes the summary."""

    CLIENT_SUMMARY = "client-summary"  # Pincer's summary model (any endpoint)
    ANTHROPIC_SERVER = "anthropic-server"  # on-demand compaction on the conversation's own model


@dataclass(frozen=True)
class ServerCompaction:
    """What the Compactor needs to compact on the provider's server: the settings that say which
    endpoints do it, the leaf provider by (name, model) and whether an endpoint is free."""

    settings: Settings
    provider: Callable[[str, str], BaseLLMProvider | None]
    is_free: Callable[[str], bool]


def pick_strategy(settings: Settings, provider: str, leaf: BaseLLMProvider | None) -> CompactionStrategy:
    """The strategy for a conversation whose last request went to `provider` (served by `leaf`):
    server compaction when the endpoint is in ``server_compaction_providers`` and speaks the
    Anthropic wire, the client summary for every other endpoint."""
    if endpoint_capabilities(settings, provider).server_compaction and isinstance(leaf, AnthropicCompatibleProvider):
        return CompactionStrategy.ANTHROPIC_SERVER
    return CompactionStrategy.CLIENT_SUMMARY


class Compactor:
    """Summarizes the oldest messages of a session whose prompt grew over the limit."""

    def __init__(
        self,
        llm: BaseLLMProvider | None,
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
        server: ServerCompaction | None = None,
    ) -> None:
        self._server = server
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
        """Compact the session once its last request went over the limit; True if the history changed.

        The strategy follows the endpoint that served that request: server compaction on an endpoint
        that does it, the client summary otherwise (see `pick_strategy`).
        """
        if not self.due(last):
            return False
        request = last.request
        if self._server is not None and request is not None:
            leaf = self._server.provider(request.provider, request.model)
            if pick_strategy(self._server.settings, request.provider, leaf) is CompactionStrategy.ANTHROPIC_SERVER:
                # pick_strategy checked the leaf speaks the Anthropic wire
                leaf = cast("AnthropicCompatibleProvider", leaf)
                return await self._compact_on_server(session, last, request, leaf, self._server)
        return await self._compact_by_summary(session, last)

    async def _compact_on_server(
        self,
        session: Session,
        last: LastRequest,
        request: SentRequest,
        leaf: AnthropicCompatibleProvider,
        server: ServerCompaction,
    ) -> bool:
        """Ask the conversation's own endpoint to summarize exactly what the last request sent."""
        if await self._costs.budget_reached():  # the pre-call budget check every LLM call goes through
            logger.warning("Daily budget reached: session %s is not compacted", session.session_id)
            return False
        prefix = list(last.sent)
        if not prefix:
            return False
        if not _follows_a_summary(session.messages[len(prefix) :]):
            logger.warning(
                "Session %s: a user message follows the last request; not compacted, retried after a later turn",
                session.session_id,
            )
            return False
        try:
            response = await leaf.compact(
                prefix,
                tools=request.tools,
                model=request.model,
                system=request.system,
                reasoning_effort=request.reasoning_effort,
                thinking=request.thinking,
            )
        except Exception:
            logger.warning("Compacting session %s on the server failed", session.session_id, exc_info=True)
            return False
        await self._costs.record(
            provider=response.provider or request.provider,
            model=response.request_model or request.model,
            usage=response.usage,
            session_id=session.session_id,
            is_free=server.is_free(response.provider or request.provider),
        )
        block = response.raw[0] if response.raw and response.raw[0].get("type") == "compaction" else None
        summary = block.get("content") if block is not None else None
        if response.stop_reason != STOP_REASON_COMPACTION or block is None or not summary:
            # A short provider problem must never erase history (Q22): the next turn over the limit retries
            logger.warning(
                "Session %s: the server returned no summary (stop_reason=%s), history kept; "
                "compaction will be retried after a later turn",
                session.session_id,
                response.stop_reason,
            )
            return False
        replacement = LLMMessage(
            role=MessageRole.SYSTEM, content=SUMMARY_PREFIX + summary, raw=[block], wire=response.wire
        )
        if not await self._sessions.compact(session, prefix, replacement, kept_ok=_follows_a_summary):
            logger.info("Session %s changed while it was being compacted; compaction discarded", session.session_id)
            return False
        logger.info("Session %s compacted on the server: %d messages summarized", session.session_id, len(prefix))
        await self._remember(session, summary)
        return True

    async def _compact_by_summary(self, session: Session, last: LastRequest) -> bool:
        """Replace the oldest messages by a summary written by the summary model; without one,
        change nothing.

        The summary call runs without the session's turn lock, so a turn arriving meanwhile
        runs on the full history; the swap then applies only if those messages are still
        the session's oldest. True if the history changed.
        """
        llm = self._llm
        if llm is None:
            logger.info("Session %s: no summary model, history kept", session.session_id)
            return False
        cut = choose_cut(session.messages, last, self._to_tokens)
        if not cut:
            return False
        prefix = list(session.messages[:cut])
        summary = await self._summarize(session, llm, prefix)
        if not summary:
            # A short provider error must never erase history (Q22): the next turn over the limit retries
            logger.warning(
                "Session %s: no summary, history kept; compaction will be retried after a later turn",
                session.session_id,
            )
            return False
        replacement = LLMMessage(role=MessageRole.SYSTEM, content=SUMMARY_PREFIX + summary)
        if not await self._sessions.compact(session, prefix, replacement):
            logger.info("Session %s changed while it was being compacted; compaction discarded", session.session_id)
            return False
        logger.info("Session %s compacted: %d oldest messages summarized", session.session_id, cut)
        await self._remember(session, summary)
        return True

    async def _summarize(self, session: Session, llm: BaseLLMProvider, messages: list[LLMMessage]) -> str:
        """The summary text, or "" when it cannot be had (budget spent, call failed)."""
        if await self._costs.budget_reached():  # the pre-call budget check every LLM call goes through
            logger.warning("Daily budget reached: session %s is not compacted", session.session_id)
            return ""
        request = LLMMessage(role=MessageRole.USER, content=f"Summarize this conversation:\n\n{_transcript(messages)}")
        try:
            response = await llm.complete(
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
) -> Compactor:
    """The compactor `pincer run`, `pincer chat` and the API agent use.

    Endpoints in ``server_compaction_providers`` compact on their own server, on the conversation's
    model; every other endpoint uses the summary model. Without one (a warning at startup) only the
    first kind works, and a session on another endpoint keeps its history until it is built.
    """
    from pincer.config.llm import call_effort, call_thinking
    from pincer.llm.router import summary_target

    if settings.summary_threshold is not None:
        logger.warning(SUMMARY_THRESHOLD_DEPRECATED)
    llm: BaseLLMProvider | None
    model: str | None
    try:
        llm, model = router.get_summarizer()
        provider, _ = summary_target(settings)
    except ValueError as e:
        logger.warning("History compaction by summary is off: %s", e)
        llm, model, provider = None, None, settings.default_provider
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
        # Resolved when a compaction runs: the leaf that served the conversation's last request
        server=ServerCompaction(
            settings,
            lambda name, model_hint: router.get_provider(name, model_hint),
            lambda name: router.is_free(name),
        ),
    )
