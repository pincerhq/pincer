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

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pincer.core.onboarding import (
    EXTRACTION_PROMPT_TEMPLATE,
    ONBOARDING_FOLLOWUP_INSTRUCTION,
    ONBOARDING_QUESTION_EN,
    PROFILE_USAGE_INSTRUCTION,
)
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
from pincer.llm.router import LLMRouter
from pincer.memory.base import (
    PINCER_MEMORY_ASSISTANT_RESPONSE_PREFIX,
    PINCER_MEMORY_USER_REQUEST_PREFIX,
)

# Signature: (tool_name, arguments, user_id, channel) -> approved?
ApprovalCallback = Callable[[str, dict[str, Any], str, str], Awaitable[bool]]

# Signature: (phase, tool_name, arguments, user_id, channel) -> None
# phase is "start" (before execution) or "end" (after execution, success or error).
ToolEventCallback = Callable[[str, str, dict[str, Any], str, str], Awaitable[None]]

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pincer.config import Settings
    from pincer.core.session import Session, SessionManager
    from pincer.llm.cost_tracker import CostTracker
    from pincer.mcp.manager import MCPClientManager
    from pincer.mcp.server import PincerMCPServer
    from pincer.memory.base import BaseMemoryBackend
    from pincer.memory.summarizer import Summarizer
    from pincer.tools.registry import ToolRegistry
    from pincer.tools.skills.index import SkillIndex

logger = logging.getLogger(__name__)


_MAX_CONSECUTIVE_ERRORS = 3
_MAX_SANITIZE_ATTEMPTS = 2

_GOOGLE_TOOL_CHAIN_HINTS = """
## Google Workspace Tool Chains

When the user asks to download or save an email attachment:
1. google__search_messages → find the email (by sender, subject, keywords, "has:attachment")
2. google__get_message → get full details including attachment metadata (attachment_id)
3. google__get_attachment → download the file using message_id + attachment_id from step 2
4. Report the downloaded file to the user

### Searching Google Drive
Use friendly parameters — do NOT write raw Drive API query syntax:
  google__search_drive_files(name="budget")
  google__search_drive_files(name="report", file_type="spreadsheet")
  google__search_drive_files(name="proposal", file_type="document")
  google__search_drive_files(name="Projects", file_type="folder")

The results include file type labels and tell you which tool to use next:
  - Google Sheet → google__get_sheet_values(spreadsheet_id="<ID>")
  - Google Doc   → google__get_doc_content(file_id="<ID>")
  - .xlsx / PDF  → google__download_file(file_id="<ID>")
  - Google Doc/Sheet/Slide (to export) → google__export_google_doc(file_id="<ID>", export_format="pdf")

When the user asks to download or save a file from Google Drive:
1. google__search_drive_files(name="<filename>") → find the file
2. Check the type label in the result:
   - Google Sheet/Doc/Slide → google__export_google_doc(file_id="...", export_format="pdf")
   - PDF, image, ZIP, .xlsx → google__download_file(file_id="...")
3. Both tools save the file to disk and return the path — do NOT use python_exec.

When the user says "export as PDF" or "save as DOCX":
→ google__export_google_doc(file_id="...", export_format="pdf" or "docx")

NEVER use python_exec or shell_exec for Gmail/Google Workspace/Drive operations.
The google__* tools have proper OAuth credentials.
The sandbox does NOT have Google credentials or libraries installed.

When searching for emails with attachments, include "has:attachment" in the query.

When the user asks to upload a local file to Google Drive:
1. If you know the exact path → google__upload_file(file_path="<path>", folder_id="...")
2. If you don't know the path → google__list_local_files(pattern="<name>") to find it
3. If the file was just downloaded → it's in ~/.pincer/downloads/ (use that path)
4. If the file was created by python_exec or file_write → it's in ~/.pincer/workspace/
5. If still not found → ask the user for the exact file path

To upload into a specific folder:
1. google__search_drive_files(name="Projects", file_type="folder") → get folder_id
2. google__upload_file(file_path="...", folder_id="<folder_id>")

NEVER use python_exec or shell_exec for Google Drive uploads.
The google__upload_file tool has proper OAuth credentials. The sandbox does NOT.

### Google Sheets — Reading Data
google__get_sheet_values works ONLY on Google Sheets (online). NOT on .xlsx files.
The search result tells you: "Google Sheet" → use get_sheet_values; "Excel (.xlsx)" → download first.

When the user asks to read, show, or display data from a Google Sheet:
1. google__search_drive_files(name="<spreadsheet name>", file_type="spreadsheet") → get spreadsheet_id
2. Optionally: google__list_sheets(spreadsheet_id="...") → get tab/sheet names
3. google__get_sheet_values(spreadsheet_id="...", sheet_name="Budget", columns="A")

Friendly parameter examples (no A1 notation needed):
  - "Show column A from Budget tab"  → sheet_name="Budget", columns="A"
  - "Show columns A through C"       → columns="A:C"
  - "Show rows 1 to 50"              → rows="1:50"
  - "Show B2:D20 from Sheet1"        → range_="Sheet1!B2:D20"
  - "Show all data"                  → just pass spreadsheet_id

If a column returns only 1 row, the data is in OTHER columns — read all columns next:
  google__get_sheet_values(spreadsheet_id="...")

### Google Sheets — Searching
When the user asks to find or locate a value in a spreadsheet:
1. google__search_drive_files(name="<name>", file_type="spreadsheet") → get spreadsheet_id
2. google__search_sheet_values(spreadsheet_id="...", search_value="SH-05")
   → returns cell references like "Sheet1!C14" with row context

NEVER use python_exec for Google Sheets operations.
The sandbox has no gspread, no openpyxl, no pandas Google Sheets support, and no Google credentials.
"""

_GOOGLE_FALLBACK_PATTERNS = [
    "gmail",
    "googleapiclient",
    "google.auth",
    "google_auth_oauthlib",
    "oauth2client",
    "imap.gmail.com",
    "smtp.gmail.com",
    "googleapis.com",
    "google_tokens.json",
    # Drive download patterns
    "drive.google.com",
    "files().get_media",
    "files().export",
    "mediaiobased",
    "service.files()",
    # Drive upload patterns
    "mediafileupload",
    "files().create(",
    "media_body",
    # Sheets patterns
    "gspread",
    "openpyxl",
    "spreadsheets().values()",
    "spreadsheets().get(",
    "col_values(",
    "row_values(",
    "worksheet(",
    "open_by_key(",
    "open_by_url(",
    # pandas reading Google Sheets or Excel
    "pd.read_excel",
    "read_excel(",
]


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


def _clean_profile_value(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in {"null", "none", "n/a", "unknown"}:
        return None
    return s


def _safe_json_loads(raw: str) -> Any:
    """Parse JSON tolerantly — strip stray markdown fences a model might add."""
    import re

    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.MULTILINE).strip()
    try:
        return json.loads(s)
    except Exception:
        return None


def _sanitize_tool_pairs(messages: list[LLMMessage]) -> list[LLMMessage]:
    """Remove orphaned tool_use and tool_result messages.

    Handles both directions:
    - tool_result without a preceding assistant tool_use
    - assistant tool_use without following tool_result(s)
    """
    all_result_ids: set[str] = set()
    for msg in messages:
        if msg.role == MessageRole.TOOL_RESULT and msg.tool_call_id:
            all_result_ids.add(msg.tool_call_id)

    clean: list[LLMMessage] = []
    for msg in messages:
        if msg.role == MessageRole.ASSISTANT and msg.tool_calls:
            orphaned_calls = [tc for tc in msg.tool_calls if tc.id not in all_result_ids]
            if orphaned_calls:
                orphan_ids = [tc.id for tc in orphaned_calls]
                logger.warning("Stripping orphaned tool_use(s): %s", orphan_ids)
                surviving = [tc for tc in msg.tool_calls if tc.id in all_result_ids]
                if surviving:
                    clean.append(
                        LLMMessage(
                            role=msg.role,
                            content=msg.content,
                            tool_calls=surviving,
                        )
                    )
                elif msg.content:
                    clean.append(LLMMessage(role=msg.role, content=msg.content))
                continue

        if msg.role == MessageRole.TOOL_RESULT:
            has_matching_use = any(
                prev.role == MessageRole.ASSISTANT and any(tc.id == msg.tool_call_id for tc in prev.tool_calls)
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
        memory_store: BaseMemoryBackend | None = None,
        summarizer: Summarizer | None = None,
        approval_callback: ApprovalCallback | None = None,
        tool_event_callback: ToolEventCallback | None = None,
    ) -> None:
        self._settings = settings
        self._llm = llm
        self._sessions = session_manager
        self._costs = cost_tracker
        self._tools = tool_registry
        self._memory = memory_store
        self._summarizer = summarizer
        self._approval_callback = approval_callback
        self._tool_event_callback = tool_event_callback
        self.mcp_manager: MCPClientManager | None = None  # set by cli after startup
        self.mcp_server: PincerMCPServer | None = None  # set by cli after startup
        self.skill_index: SkillIndex | None = None  # set by cli after startup
        self.mcp_shell: Any = None  # set by EmbeddedMCPShell.start()
        # (user_id, channel_name) of the most recent message — used by ask_user
        self._last_active: tuple[str, str] | None = None
        # Injected by cli.py: (user_id, channel_name, question) -> answer
        self._ask_user_callback: Callable[[str, str, str], Awaitable[str]] | None = None

    @property
    def tool_registry(self) -> ToolRegistry:
        return self._tools

    async def _ask_user_on_active_channel(self, question: str) -> str:
        """
        Send a question to the user's most-recently active channel and await their reply.

        Called by the MCP server when an external client invokes `pincer_ask_user`.
        Returns the user's response, or a descriptive message on timeout / no channel.
        """
        if not self._ask_user_callback or not self._last_active:
            return "[No active messaging channel — user unreachable]"
        user_id, channel_name = self._last_active
        try:
            return await self._ask_user_callback(user_id, channel_name, question)
        except Exception as e:
            logger.exception("ask_user callback failed")
            return f"[Error contacting user: {e}]"

    async def handle_message(
        self,
        user_id: str,
        channel: str,
        text: str,
        images: list[tuple[bytes, str]] | None = None,
        channel_user_id: str | None = None,
        channel_name: str | None = None,
    ) -> AgentResponse:
        """
        Main entry point: process a user message and return agent's response.

        Args:
            user_id: Canonical pincer_user_id
            channel: Session key (used for session isolation).  For most
                channels this is the channel name.  For channels where the
                session key differs from the channel name (e.g. Teams per-thread
                keys), pass the session key here and the stable channel name via
                ``channel_name``.
            text: User's message text
            images: Optional list of (raw_bytes, media_type) tuples
            channel_user_id: Stable channel-specific user ID (e.g. phone number,
                Telegram numeric ID).  Used to tag memories with both
                ``user:{user_id}`` and ``user:{channel_name}:{channel_user_id}``
                so records are findable by either identifier, and passed to
                tool execution so channel-bound tools (``send_file``,
                ``send_image``) address the user's real channel ID instead of
                the cross-channel canonical ``user_id``.
            channel_name: Stable channel name for memory tagging when it differs
                from the session key.  Defaults to ``channel`` when omitted.
        """
        self._last_active = (user_id, channel_name or channel)
        session = await self._sessions.get_or_create(user_id, channel)

        # Snapshot onboarding state BEFORE the user message is appended.
        was_fresh = session.is_fresh()
        awaiting_onboarding_reply = session.onboarding_prompt_already_sent() and not session.is_onboarded()

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

        # Build system prompt with relevant memories — inject the onboarding
        # followup instruction only on the turn that handles the user's reply.
        extra_system = ONBOARDING_FOLLOWUP_INSTRUCTION if awaiting_onboarding_reply else None
        system_prompt = await self._build_system_prompt(user_id, text, extra_system=extra_system)

        # Get tool schemas
        tool_schemas = self._tools.get_schemas() if self._tools.has_tools else None
        logger.debug(
            "Tools available: %s",
            [t.get("name") for t in (tool_schemas or [])],
        )

        total_cost = 0.0
        tool_calls_count = 0
        final_text = ""
        last_response: LLMResponse | None = None
        consecutive_errors = 0
        sanitize_attempts = 0

        # ── ReAct Loop ───────────────────────────────────
        for _iteration in range(self._settings.max_tool_iterations):
            # Proactive sanitization: fix orphaned pairs before they reach the API
            clean = _sanitize_tool_pairs(session.messages)
            if len(clean) != len(session.messages):
                logger.info("Proactive sanitization removed %d orphaned messages", len(session.messages) - len(clean))
                session.messages = clean
                await self._sessions._persist(session)  # noqa: SLF001

            try:
                response: LLMResponse = await self._llm.complete(
                    messages=session.messages,
                    tools=tool_schemas,
                    system=system_prompt,
                )
                last_response = response
                # By the second iteration the model has already either
                # committed to a tool call or produced final text — the
                # image no longer needs to be resent (and, unlike text,
                # isn't persisted across turns anyway; see LLMMessage.to_dict).
                # Cuts vision re-ingestion cost on every retry within this loop.
                if user_msg.images:
                    user_msg.images = []
            except BudgetExceededError:
                final_text = (
                    "Warning: Daily budget limit reached. "
                    f"Limit: ${self._settings.daily_budget_usd:.2f}. "
                    "I'll be back tomorrow, or you can increase the limit."
                )
                break
            except LLMError as e:
                if "tool_use" in str(e) and "tool_result" in str(e):
                    sanitize_attempts += 1
                    if sanitize_attempts > _MAX_SANITIZE_ATTEMPTS:
                        logger.warning("Sanitization failed after %d attempts, clearing session", sanitize_attempts - 1)
                        session.messages = [m for m in session.messages if m.role == MessageRole.SYSTEM]
                        await self._sessions._persist(session)  # noqa: SLF001
                        final_text = "I had a session error and cleared my context. Please resend your message."
                        break
                    logger.warning("Orphaned tool pair detected, sanitizing session (attempt %d)", sanitize_attempts)
                    session.messages = _sanitize_tool_pairs(session.messages)
                    await self._sessions._persist(session)  # noqa: SLF001
                    continue
                raise

            # Track cost
            try:
                # Attribute cost to the provider that actually served (may be a
                # failover), not the configured primary.
                provider = response.provider or self._settings.default_provider
                is_free = self._llm.is_free(provider) if isinstance(self._llm, LLMRouter) else False
                cost = await self._costs.record(
                    provider=provider,
                    model=response.model,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    session_id=session.session_id,
                    is_free=is_free,
                )
                total_cost += cost
            except BudgetExceededError as e:
                final_text = f"Warning: Budget limit reached (${e.spent:.2f}/${e.limit:.2f}). Stopping."
                break

            # If the LLM wants to use tools
            if response.has_tool_calls:
                logger.info(
                    "LLM requested tools: %s",
                    [tc.name for tc in response.tool_calls],
                )
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
                    result = await self._execute_tool(tool_call, user_id, channel, channel_name, channel_user_id)

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

        # ── Onboarding: append warm question on a fresh session ────────────
        if was_fresh and not session.is_onboarded() and not session.onboarding_prompt_already_sent() and final_text:
            final_text = final_text.rstrip() + "\n\n" + ONBOARDING_QUESTION_EN
            session.mark_onboarding_prompt_sent()
            logger.info("onboarding.prompt_sent user=%s channel=%s", user_id, channel)

        # Save final assistant message
        if final_text:
            final_msg = LLMMessage(role=MessageRole.ASSISTANT, content=final_text)
            await self._sessions.add_message(session, final_msg)

        # ── Onboarding: parse the user's reply and close the gate ─────────
        if awaiting_onboarding_reply:
            await self._ingest_onboarding_reply(session, text, user_id, channel)

        # Store the final exchange as a memory for future retrieval
        if self._memory and final_text:
            try:
                tag_channel = channel_name or channel
                extra_tags = [f"user:{tag_channel}:{channel_user_id}"] if channel_user_id else None
                await self._memory.store_memory(
                    user_id=user_id,
                    content=(
                        f"{PINCER_MEMORY_USER_REQUEST_PREFIX}: {text}\n"
                        f"{PINCER_MEMORY_ASSISTANT_RESPONSE_PREFIX}: {final_text}"
                    ),
                    category="exchange",
                    extra_tags=extra_tags,
                )
            except Exception:
                logger.debug("Failed to store exchange memory", exc_info=True)

        return AgentResponse(
            text=final_text,
            cost_usd=total_cost,
            tool_calls_made=tool_calls_count,
            model=last_response.model if last_response else "",
        )

    async def run_headless(
        self,
        prompt: str,
        user_id: str,
        channel: str,
        *,
        channel_name: str | None = None,
        channel_user_id: str | None = None,
        allowed_tools: list[str] | None = None,
        max_iterations: int | None = None,
    ) -> str:
        """Run a prompt through the LLM with tool access, outside any session.

        For proactive/scheduled work (see ``ProactiveAgent.run_custom_action``)
        — no ``Session`` is created or persisted, no long-term memory is
        searched or written, and no onboarding logic runs. Tool calls execute
        with ``skip_approval=True`` since there's no live user to approve
        anything; the one-time consent is the ``schedule_create`` call that
        set this prompt up in the first place.

        ``allowed_tools``, when given, restricts which tool schemas the LLM
        even sees (enforced by omission, not by intercepting calls after the
        fact) — ``None`` means the full tool surface, same as live chat.
        """
        messages: list[LLMMessage] = [LLMMessage(role=MessageRole.USER, content=prompt)]
        system_prompt = await self._build_system_prompt(user_id, prompt, skip_memory=True)

        tool_schemas = self._tools.get_schemas() if self._tools.has_tools else None
        if tool_schemas is not None and allowed_tools is not None:
            tool_schemas = [s for s in tool_schemas if s.get("name") in allowed_tools] or None

        final_text = ""
        last_response: LLMResponse | None = None
        consecutive_errors = 0
        iterations = max_iterations or self._settings.max_tool_iterations

        for _iteration in range(iterations):
            try:
                response: LLMResponse = await self._llm.complete(
                    messages=messages,
                    tools=tool_schemas,
                    system=system_prompt,
                )
                last_response = response
            except BudgetExceededError:
                return (
                    "Warning: Daily budget limit reached. "
                    f"Limit: ${self._settings.daily_budget_usd:.2f}. "
                    "Could not complete this scheduled action."
                )
            except LLMError:
                logger.exception("run_headless: LLM call failed")
                return "Sorry, I couldn't complete this scheduled action due to an LLM error."

            try:
                provider = response.provider or self._settings.default_provider
                is_free = self._llm.is_free(provider) if isinstance(self._llm, LLMRouter) else False
                await self._costs.record(
                    provider=provider,
                    model=response.model,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    session_id=None,
                    is_free=is_free,
                )
            except BudgetExceededError as e:
                return f"Warning: Budget limit reached (${e.spent:.2f}/${e.limit:.2f}). Stopping."

            if response.has_tool_calls:
                messages.append(
                    LLMMessage(role=MessageRole.ASSISTANT, content=response.content, tool_calls=response.tool_calls)
                )

                iteration_had_error = False
                for tool_call in response.tool_calls:
                    result = await self._execute_tool(
                        tool_call, user_id, channel, channel_name, channel_user_id, skip_approval=True
                    )
                    messages.append(
                        LLMMessage(
                            role=MessageRole.TOOL_RESULT,
                            content=result.content,
                            tool_call_id=result.tool_call_id,
                        )
                    )
                    if result.is_error:
                        iteration_had_error = True

                if iteration_had_error:
                    consecutive_errors += 1
                    if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                        logger.warning(
                            "run_headless: circuit breaker after %d consecutive tool errors", consecutive_errors
                        )
                        return (
                            response.content
                            if response.content
                            else "I ran into repeated tool failures completing this scheduled action."
                        )
                else:
                    consecutive_errors = 0

                continue

            final_text = response.content
            break
        else:
            final_text = (
                last_response.content
                if last_response and last_response.content
                else "I seem to be going in circles trying to complete this scheduled action."
            )

        return final_text

    async def handle_message_stream(
        self,
        user_id: str,
        channel: str,
        text: str,
        images: list[tuple[bytes, str]] | None = None,
        channel_user_id: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """
        Process a user message, yielding StreamChunks as the response is generated.

        Tool-call iterations use complete() (non-streaming). Only the final
        text response is streamed token-by-token.

        ``channel_user_id``, when given, is the channel-native ID and is
        passed to tool execution so channel-bound tools (``send_file``,
        ``send_image``) address the user's real channel ID instead of the
        cross-channel canonical ``user_id`` — see ``handle_message``.
        """
        self._last_active = (user_id, channel)
        session = await self._sessions.get_or_create(user_id, channel)

        was_fresh = session.is_fresh()
        awaiting_onboarding_reply = session.onboarding_prompt_already_sent() and not session.is_onboarded()

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

        if self._summarizer:
            await self._summarizer.maybe_summarize(session)

        extra_system = ONBOARDING_FOLLOWUP_INSTRUCTION if awaiting_onboarding_reply else None
        system_prompt = await self._build_system_prompt(user_id, text, extra_system=extra_system)
        tool_schemas = self._tools.get_schemas() if self._tools.has_tools else None
        logger.debug(
            "Tools available: %s",
            [t.get("name") for t in (tool_schemas or [])],
        )
        consecutive_errors = 0
        circuit_broken = False
        sanitize_attempts = 0
        response: LLMResponse | None = None

        # Tool-call iterations (non-streaming)
        for _iteration in range(self._settings.max_tool_iterations):
            # Proactive sanitization: fix orphaned pairs before they reach the API
            clean = _sanitize_tool_pairs(session.messages)
            if len(clean) != len(session.messages):
                logger.info("Proactive sanitization removed %d orphaned messages", len(session.messages) - len(clean))
                session.messages = clean
                await self._sessions._persist(session)  # noqa: SLF001

            try:
                response = await self._llm.complete(
                    messages=session.messages,
                    tools=tool_schemas,
                    system=system_prompt,
                )
                # See handle_message: drop the image after the first
                # completion so later retries in this loop don't resend it.
                if user_msg.images:
                    user_msg.images = []
            except BudgetExceededError:
                yield StreamChunk(StreamEventType.DONE, "Daily budget limit reached.")
                return
            except LLMError as e:
                if "tool_use" in str(e) and "tool_result" in str(e):
                    sanitize_attempts += 1
                    if sanitize_attempts > _MAX_SANITIZE_ATTEMPTS:
                        logger.warning("Sanitization failed after %d attempts, clearing session", sanitize_attempts - 1)
                        session.messages = [m for m in session.messages if m.role == MessageRole.SYSTEM]
                        await self._sessions._persist(session)  # noqa: SLF001
                        yield StreamChunk(
                            StreamEventType.DONE,
                            "I had a session error and cleared my context. Please resend your message.",
                        )
                        return
                    logger.warning("Orphaned tool pair detected, sanitizing session (attempt %d)", sanitize_attempts)
                    session.messages = _sanitize_tool_pairs(session.messages)
                    await self._sessions._persist(session)  # noqa: SLF001
                    continue
                raise

            if not response.has_tool_calls:
                break

            logger.info(
                "LLM requested tools: %s",
                [tc.name for tc in response.tool_calls],
            )
            assistant_msg = LLMMessage(
                role=MessageRole.ASSISTANT,
                content=response.content,
                tool_calls=response.tool_calls,
            )
            await self._sessions.add_message(session, assistant_msg)

            iteration_had_error = False
            for tool_call in response.tool_calls:
                yield StreamChunk(StreamEventType.TOOL_START, f"Using {tool_call.name}...")
                result = await self._execute_tool(tool_call, user_id, channel, channel_user_id=channel_user_id)
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
            fallback_content = response.content if response and response.content else "Max iterations reached."
            yield StreamChunk(StreamEventType.DONE, fallback_content)
            return

        if circuit_broken:
            fallback = (
                response.content
                if response and response.content
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

        # ── Onboarding: append warm question on a fresh session ────────────
        if was_fresh and not session.is_onboarded() and not session.onboarding_prompt_already_sent() and full_text:
            tail = "\n\n" + ONBOARDING_QUESTION_EN
            full_text = full_text.rstrip() + tail
            session.mark_onboarding_prompt_sent()
            logger.info("onboarding.prompt_sent user=%s channel=%s", user_id, channel)
            yield StreamChunk(StreamEventType.TEXT, tail)

        if full_text:
            final_msg = LLMMessage(role=MessageRole.ASSISTANT, content=full_text)
            await self._sessions.add_message(session, final_msg)

        # ── Onboarding: parse the user's reply and close the gate ─────────
        if awaiting_onboarding_reply:
            await self._ingest_onboarding_reply(session, text, user_id, channel)

        yield StreamChunk(StreamEventType.DONE, full_text)

    async def _build_system_prompt(
        self,
        user_id: str,
        user_text: str,
        *,
        extra_system: str | None = None,
        skip_memory: bool = False,
    ) -> str:
        """Build system prompt, injecting relevant memories if available."""
        base_prompt = self._settings.system_prompt + "\n\n" + PROFILE_USAGE_INSTRUCTION

        # Phone-call guidance when make_phone_call tool is available
        if "make_phone_call" in self._tools.list_tools():
            base_prompt += (
                "\n\nWhen the user asks you to call a phone number, you MUST use the "
                "make_phone_call tool. Do not describe or simulate the call in your response — call the tool."
            )

        # Google Workspace tool chain hints when google__ tools are registered
        if any(t.startswith("google__") for t in self._tools.list_tools()):
            base_prompt += "\n" + _GOOGLE_TOOL_CHAIN_HINTS

        # Inject available skills (progressive disclosure level 1)
        if self.skill_index:
            skills_block = self.skill_index.render_prompt_block(max_tokens=self._settings.skills_max_prompt_tokens)
            if skills_block:
                base_prompt += (
                    "\n\n[Available Skills — call load_skill(name) to read one before using its scripts]\n"
                    + skills_block
                )

        # Inject connected MCP server info when available
        if self.mcp_manager:
            try:
                servers = await self.mcp_manager.list_servers()
                connected = [s for s in servers if s["connected"]]
                if connected:
                    max_chars = self._settings.mcp_instructions_max_chars
                    server_lines = []
                    for s in connected:
                        server_lines.append(f"  - {s['name']} ({s['tool_count']} tool(s))")
                        instructions = s.get("instructions")
                        if instructions:
                            collapsed = " ".join(instructions.split())
                            if max_chars and len(collapsed) > max_chars:
                                collapsed = collapsed[:max_chars] + "…"
                            server_lines.append(f"    → {collapsed}")
                    base_prompt += "\n\n[Connected MCP servers — use their prefixed tools when relevant]\n" + "\n".join(
                        server_lines
                    )
            except Exception:
                logger.debug("Failed to fetch MCP server list for prompt", exc_info=True)

        def _with_extra(prompt: str) -> str:
            return f"{prompt}\n\n{extra_system}" if extra_system else prompt

        if not self._memory or skip_memory:
            return _with_extra(base_prompt)

        try:
            memories = await self._memory.search_text(user_text, user_id=user_id, limit=3)
            if not memories:
                return _with_extra(base_prompt)

            memory_lines = [f"- {m.content}" for m in memories]
            memory_block = "\n".join(memory_lines)
            return _with_extra(f"{base_prompt}\n\n[Relevant memories about this user]\n{memory_block}")
        except Exception:
            logger.debug("Failed to fetch memories for prompt", exc_info=True)
            return _with_extra(base_prompt)

    async def _ingest_onboarding_reply(
        self,
        session: Session,
        reply_text: str,
        user_id: str,
        channel: str,
    ) -> None:
        """Parse name / use_case / language from the user's reply and persist.

        The onboarding gate is ALWAYS closed afterward, even on parse failure,
        so the user never sees the onboarding question a second time.
        """
        name: str | None = None
        use_case: str | None = None
        language: str | None = None

        try:
            extraction_msg = LLMMessage(
                role=MessageRole.USER,
                content=EXTRACTION_PROMPT_TEMPLATE.format(reply=reply_text[:2000]),
            )
            response = await self._llm.complete(
                messages=[extraction_msg],
                max_tokens=200,
                temperature=0,
            )
            data = _safe_json_loads(response.content or "")
            if isinstance(data, dict):
                name = _clean_profile_value(data.get("name"))
                use_case = _clean_profile_value(data.get("use_case"))
                language = _clean_profile_value(data.get("language"))
        except Exception as e:
            logger.warning("Onboarding extraction failed (closing gate anyway): %s", e)

        # ALWAYS close the gate, even with no captured fields.
        session.mark_onboarded(name=name, use_case=use_case, language=language)
        await self._sessions._persist(session)  # noqa: SLF001

        logger.info(
            "onboarding.completed user=%s channel=%s fields=%s",
            user_id,
            channel,
            [k for k, v in (("name", name), ("use_case", use_case), ("language", language)) if v],
        )

        if self._memory and any([name, use_case, language]):
            try:
                await self._memory.add_profile(
                    user_id=user_id,
                    name=name,
                    use_case=use_case,
                    language=language,
                )
            except Exception:
                logger.debug("Failed to write profile memory", exc_info=True)

    async def _execute_tool(
        self,
        tool_call: ToolCall,
        user_id: str,
        channel: str,
        channel_name: str | None = None,
        channel_user_id: str | None = None,
        *,
        skip_approval: bool = False,
    ) -> ToolResult:
        """Execute a single tool call, catching errors.

        ``channel_user_id``, when given, is the channel-native ID (e.g. a
        Telegram numeric ID or WhatsApp JID) and takes priority over the
        canonical ``user_id`` for the tool-execution context — channel-bound
        tools like ``send_file``/``send_image`` need the native ID to actually
        address the user, not the cross-channel canonical identity.

        If the tool requires approval and a callback is configured, the user
        is prompted first.  Without a callback the tool still runs (backward
        compat with shell_require_approval) but a warning is logged.

        ``skip_approval``, when True, bypasses the approval gate entirely
        regardless of ``require_approval``/``approval_required`` config.
        Only ``run_headless`` (proactive/scheduled execution, no live user
        to approve anything) is allowed to set this — never from
        ``handle_message``/``handle_message_stream``.
        """
        logger.info("Tool call: %s(%s)", tool_call.name, tool_call.arguments)

        # Guard: block Google-related code in general-purpose execution tools
        if tool_call.name in ("python_exec", "shell_exec"):
            code = tool_call.arguments.get("code", "") or tool_call.arguments.get("command", "")
            if any(p in code.lower() for p in _GOOGLE_FALLBACK_PATTERNS):
                return ToolResult(
                    tool_call_id=tool_call.id,
                    content=json.dumps(
                        {
                            "error": (
                                "Do not use python_exec/shell_exec for Google Workspace operations. "
                                "The sandbox has no Google credentials or libraries (no gspread, no openpyxl). "
                                "Use the native google__* tools instead. "
                                "For Gmail attachments: google__search_messages"
                                " → google__get_message → google__get_attachment. "
                                "For Drive regular files: google__download_file(file_id=...). "
                                "For Drive Google Docs/Sheets/Slides: "
                                "google__export_google_doc(file_id=..., export_format='pdf'). "
                                "For Drive uploads: google__upload_file(file_path=...). "
                                "If you don't know the file path, call google__list_local_files() first. "
                                "For Sheets: google__get_sheet_values("
                                "spreadsheet_id=..., sheet_name=..., columns=...). "
                                "For Sheets search: google__search_sheet_values("
                                "spreadsheet_id=..., search_value=...). "
                                "Get spreadsheet_id via google__search_drive_files("
                                "name='<name>', file_type='spreadsheet')."
                            )
                        }
                    ),
                    is_error=True,
                )

        if not skip_approval and self._tools.requires_approval(tool_call.name):
            if self._approval_callback:
                try:
                    approved = await self._approval_callback(
                        tool_call.name,
                        tool_call.arguments,
                        user_id,
                        channel_name or channel,
                    )
                except Exception:
                    logger.exception("Approval callback failed for '%s'", tool_call.name)
                    approved = False
                if not approved:
                    return ToolResult(
                        tool_call_id=tool_call.id,
                        content=f"Action '{tool_call.name}' was declined by the user.",
                        is_error=True,
                    )
            else:
                logger.warning(
                    "Tool '%s' requires approval but no callback is configured; auto-approving.",
                    tool_call.name,
                )

        await self._fire_tool_event("start", tool_call, user_id, channel)
        try:
            try:
                result_text = await self._tools.execute(
                    tool_call.name,
                    tool_call.arguments,
                    context={
                        "user_id": channel_user_id or user_id,
                        "channel": channel,
                        "pincer_user_id": user_id,
                        "channel_name": channel_name or channel,
                    },
                )
                if tool_call.name == "make_phone_call":
                    logger.info(
                        "make_phone_call result: %s",
                        (result_text[:200] + "..." if len(result_text) > 200 else result_text)
                        if result_text
                        else "(empty)",
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
        finally:
            await self._fire_tool_event("end", tool_call, user_id, channel)

    async def _fire_tool_event(
        self,
        phase: str,
        tool_call: ToolCall,
        user_id: str,
        channel: str,
    ) -> None:
        if not self._tool_event_callback:
            return
        try:
            await self._tool_event_callback(
                phase,
                tool_call.name,
                tool_call.arguments,
                user_id,
                channel,
            )
        except Exception:
            logger.debug("tool_event_callback failed", exc_info=True)
