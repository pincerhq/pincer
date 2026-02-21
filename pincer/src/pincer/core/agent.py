"""
The core agent brain — implements a ReAct (Reason + Act) loop.

Flow:
1. Receive user message
2. Load session history
3. Send to LLM with available tools
4. If LLM returns tool_call -> execute -> feed result -> goto 3
5. If LLM returns text -> return to user
6. Save session
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from pincer.exceptions import BudgetExceededError, LLMError, ToolNotFoundError
from pincer.llm.base import (
    BaseLLMProvider,
    ImageContent,
    LLMMessage,
    LLMResponse,
    MessageRole,
    ToolCall,
    ToolResult,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pincer.config import Settings
    from pincer.core.session import SessionManager
    from pincer.llm.cost_tracker import CostTracker
    from pincer.memory.store import MemoryStore
    from pincer.memory.summarizer import Summarizer
    from pincer.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

_MAX_CONSECUTIVE_ERRORS = 3


class StreamEventType(StrEnum):
    TEXT = "text"
    TOOL_START = "tool_start"
    TOOL_DONE = "tool_done"
    DONE = "done"


@dataclass(frozen=True, slots=True)
class StreamChunk:
    """A single event in a streaming agent response."""

    type: StreamEventType
    content: str


@dataclass
class AgentResponse:
    """What the agent returns to the channel."""

    text: str
    cost_usd: float = 0.0
    tool_calls_made: int = 0
    model: str = ""


def _strip_orphaned_tool_results(messages: list[LLMMessage]) -> list[LLMMessage]:
    """Remove tool_result messages that have no preceding tool_use."""
    clean: list[LLMMessage] = []
    for i, msg in enumerate(messages):
        if msg.role == MessageRole.TOOL_RESULT:
            has_matching_use = any(
                prev.role == MessageRole.ASSISTANT
                and any(tc.id == msg.tool_call_id for tc in prev.tool_calls)
                for prev in clean
            )
            if not has_matching_use:
                logger.warning("Stripping orphaned tool_result %s", msg.tool_call_id)
                continue
        clean.append(msg)
    return clean


class Agent:
    """The core Pincer agent."""

    def __init__(
        self,
        settings: Settings,
        llm: BaseLLMProvider,
        session_manager: SessionManager,
        cost_tracker: CostTracker,
        tool_registry: ToolRegistry,
        memory_store: MemoryStore | None = None,
        summarizer: Summarizer | None = None,
    ) -> None:
        self._settings = settings
        self._llm = llm
        self._sessions = session_manager
        self._costs = cost_tracker
        self._tools = tool_registry
        self._memory = memory_store
        self._summarizer = summarizer

    async def handle_message(
        self,
        user_id: str,
        channel: str,
        text: str,
        images: list[tuple[bytes, str]] | None = None,
    ) -> AgentResponse:
        """
        Main entry point: process a user message and return agent's response.

        Args:
            user_id: Unique user identifier
            channel: Channel name (telegram, whatsapp, etc.)
            text: User's message text
            images: Optional list of (raw_bytes, media_type) tuples
        """
        session = await self._sessions.get_or_create(user_id, channel)

        # Build user message
        img_contents: list[ImageContent] = []
        if images:
            for raw, media_type in images:
                img_contents.append(ImageContent.from_bytes(raw, media_type))

        user_msg = LLMMessage(
            role=MessageRole.USER,
            content=text,
            images=img_contents,
        )
        await self._sessions.add_message(session, user_msg)

        # Summarize if conversation is getting long
        if self._summarizer:
            await self._summarizer.maybe_summarize(session)

        # Build system prompt with relevant memories
        system_prompt = await self._build_system_prompt(user_id, text)

        # Get tool schemas
        tool_schemas = self._tools.get_schemas() if self._tools.has_tools else None

        total_cost = 0.0
        tool_calls_count = 0
        final_text = ""
        last_response: LLMResponse | None = None
        consecutive_errors = 0

        # ── ReAct Loop ───────────────────────────────────
        for _iteration in range(self._settings.max_tool_iterations):
            try:
                response: LLMResponse = await self._llm.complete(
                    messages=session.messages,
                    tools=tool_schemas,
                    system=system_prompt,
                )
                last_response = response
            except BudgetExceededError:
                final_text = (
                    "Warning: Daily budget limit reached. "
                    f"Limit: ${self._settings.daily_budget_usd:.2f}. "
                    "I'll be back tomorrow, or you can increase the limit."
                )
                break
            except LLMError as e:
                if "tool_use_id" in str(e) and "tool_result" in str(e):
                    logger.warning("Orphaned tool_result detected, sanitizing session")
                    session.messages = _strip_orphaned_tool_results(session.messages)
                    await self._sessions._persist(session)  # noqa: SLF001
                    continue
                raise

            # Track cost
            try:
                cost = await self._costs.record(
                    provider=self._settings.default_provider.value,
                    model=response.model,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    session_id=session.session_id,
                )
                total_cost += cost
            except BudgetExceededError as e:
                final_text = (
                    f"Warning: Budget limit reached (${e.spent:.2f}/${e.limit:.2f}). Stopping."
                )
                break

            # If the LLM wants to use tools
            if response.has_tool_calls:
                # Save the assistant's tool-call message
                assistant_msg = LLMMessage(
                    role=MessageRole.ASSISTANT,
                    content=response.content,
                    tool_calls=response.tool_calls,
                )
                await self._sessions.add_message(session, assistant_msg)

                # Execute each tool
                iteration_had_error = False
                for tool_call in response.tool_calls:
                    tool_calls_count += 1
                    result = await self._execute_tool(tool_call, user_id, channel)

                    result_msg = LLMMessage(
                        role=MessageRole.TOOL_RESULT,
                        content=result.content,
                        tool_call_id=result.tool_call_id,
                    )
                    await self._sessions.add_message(session, result_msg)

                    if result.is_error:
                        iteration_had_error = True

                if iteration_had_error:
                    consecutive_errors += 1
                    if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                        logger.warning("Circuit breaker: %d consecutive tool errors", consecutive_errors)
                        final_text = (
                            response.content
                            if response.content
                            else "I'm having repeated tool failures. Let me respond with what I have."
                        )
                        break
                else:
                    consecutive_errors = 0

                # Continue loop — LLM will see tool results
                continue

            # No tool calls — we have the final answer
            final_text = response.content
            break
        else:
            # Loop exhausted
            final_text = (
                last_response.content
                if last_response and last_response.content
                else "I seem to be going in circles. Let me give you what I have so far."
            )

        # Save final assistant message
        if final_text:
            final_msg = LLMMessage(role=MessageRole.ASSISTANT, content=final_text)
            await self._sessions.add_message(session, final_msg)

        # Store the final exchange as a memory for future retrieval
        if self._memory and final_text:
            try:
                await self._memory.store_memory(
                    user_id=user_id,
                    content=f"User asked: {text[:200]}\nAssistant replied: {final_text[:300]}",
                    category="exchange",
                )
            except Exception:
                logger.debug("Failed to store exchange memory", exc_info=True)

        return AgentResponse(
            text=final_text,
            cost_usd=total_cost,
            tool_calls_made=tool_calls_count,
            model=last_response.model if last_response else "",
        )

    async def handle_message_stream(
        self,
        user_id: str,
        channel: str,
        text: str,
        images: list[tuple[bytes, str]] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """
        Process a user message, yielding StreamChunks as the response is generated.

        Tool-call iterations use complete() (non-streaming). Only the final
        text response is streamed token-by-token.
        """
        session = await self._sessions.get_or_create(user_id, channel)

        img_contents: list[ImageContent] = []
        if images:
            for raw, media_type in images:
                img_contents.append(ImageContent.from_bytes(raw, media_type))

        user_msg = LLMMessage(
            role=MessageRole.USER, content=text, images=img_contents,
        )
        await self._sessions.add_message(session, user_msg)

        if self._summarizer:
            await self._summarizer.maybe_summarize(session)

        system_prompt = await self._build_system_prompt(user_id, text)
        tool_schemas = self._tools.get_schemas() if self._tools.has_tools else None
        consecutive_errors = 0
        circuit_broken = False

        # Tool-call iterations (non-streaming)
        for _iteration in range(self._settings.max_tool_iterations):
            try:
                response: LLMResponse = await self._llm.complete(
                    messages=session.messages,
                    tools=tool_schemas,
                    system=system_prompt,
                )
            except BudgetExceededError:
                yield StreamChunk(StreamEventType.DONE, "Daily budget limit reached.")
                return
            except LLMError as e:
                if "tool_use_id" in str(e) and "tool_result" in str(e):
                    logger.warning("Orphaned tool_result detected, sanitizing session")
                    session.messages = _strip_orphaned_tool_results(session.messages)
                    await self._sessions._persist(session)  # noqa: SLF001
                    continue
                raise

            if not response.has_tool_calls:
                break

            assistant_msg = LLMMessage(
                role=MessageRole.ASSISTANT,
                content=response.content,
                tool_calls=response.tool_calls,
            )
            await self._sessions.add_message(session, assistant_msg)

            iteration_had_error = False
            for tool_call in response.tool_calls:
                yield StreamChunk(
                    StreamEventType.TOOL_START, f"Using {tool_call.name}..."
                )
                result = await self._execute_tool(tool_call, user_id, channel)
                result_msg = LLMMessage(
                    role=MessageRole.TOOL_RESULT,
                    content=result.content,
                    tool_call_id=result.tool_call_id,
                )
                await self._sessions.add_message(session, result_msg)
                yield StreamChunk(StreamEventType.TOOL_DONE, tool_call.name)
                if result.is_error:
                    iteration_had_error = True

            if iteration_had_error:
                consecutive_errors += 1
                if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                    logger.warning("Circuit breaker: %d consecutive tool errors", consecutive_errors)
                    circuit_broken = True
                    break
            else:
                consecutive_errors = 0
        else:
            yield StreamChunk(
                StreamEventType.DONE,
                response.content if response.content else "Max iterations reached.",
            )
            return

        if circuit_broken:
            fallback = (
                response.content
                if response.content
                else "I'm having repeated tool failures. Let me respond with what I have."
            )
            if fallback:
                final_msg = LLMMessage(role=MessageRole.ASSISTANT, content=fallback)
                await self._sessions.add_message(session, final_msg)
            yield StreamChunk(StreamEventType.DONE, fallback)
            return

        # Stream the final response
        full_text = ""
        try:
            async for token in self._llm.stream(
                messages=session.messages,
                system=system_prompt,
            ):
                full_text += token
                yield StreamChunk(StreamEventType.TEXT, token)
        except Exception:
            logger.exception("Streaming failed, falling back to complete()")
            if not full_text:
                full_text = response.content if response else ""

        if full_text:
            final_msg = LLMMessage(role=MessageRole.ASSISTANT, content=full_text)
            await self._sessions.add_message(session, final_msg)

        yield StreamChunk(StreamEventType.DONE, full_text)

    async def _build_system_prompt(self, user_id: str, user_text: str) -> str:
        """Build system prompt, injecting relevant memories if available."""
        base_prompt = self._settings.system_prompt
        if not self._memory:
            return base_prompt

        try:
            memories = await self._memory.search_text(user_text, user_id=user_id, limit=3)
            if not memories:
                return base_prompt

            memory_lines = [f"- {m.content}" for m in memories]
            memory_block = "\n".join(memory_lines)
            return (
                f"{base_prompt}\n\n"
                f"[Relevant memories about this user]\n{memory_block}"
            )
        except Exception:
            logger.debug("Failed to fetch memories for prompt", exc_info=True)
            return base_prompt

    async def _execute_tool(
        self,
        tool_call: ToolCall,
        user_id: str,
        channel: str,
    ) -> ToolResult:
        """Execute a single tool call, catching errors."""
        logger.info("Tool call: %s(%s)", tool_call.name, tool_call.arguments)
        try:
            result_text = await self._tools.execute(
                tool_call.name,
                tool_call.arguments,
                context={"user_id": user_id, "channel": channel},
            )
            return ToolResult(
                tool_call_id=tool_call.id,
                content=result_text,
                is_error=False,
            )
        except ToolNotFoundError:
            return ToolResult(
                tool_call_id=tool_call.id,
                content=f"Error: Tool '{tool_call.name}' not found.",
                is_error=True,
            )
        except Exception as e:
            logger.exception("Tool '%s' failed", tool_call.name)
            return ToolResult(
                tool_call_id=tool_call.id,
                content=f"Error executing {tool_call.name}: {type(e).__name__}: {e}",
                is_error=True,
            )
