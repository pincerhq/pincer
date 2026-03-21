"""
ApprovalBackend — pluggable user-approval interface for MCP tool execution.

When an MCP client requests a tool that requires approval, the approval backend
decides how to get the user's answer. Different backends suit different modes:

- ChannelApprovalBackend: routes to Telegram/WhatsApp (embedded agent mode)
- PolicyApprovalBackend:  auto-approve/deny from config rules (standalone headless)
- CLIApprovalBackend:     interactive terminal prompt (standalone interactive)
- WebhookApprovalBackend: POST to an external approval service (both modes)
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger("pincer.mcp.approval")


# ── Data types ────────────────────────────────────────────────────────────────


class ApprovalDecision(StrEnum):
    """Outcome of an approval request."""

    APPROVED = "approved"
    DENIED = "denied"
    TIMEOUT = "timeout"


@dataclass
class ApprovalRequest:
    """
    Describes a tool execution that requires user approval.

    Fields:
        tool_name:   Name of the tool being called.
        arguments:   Arguments the caller passed.
        source:      Identifier of the MCP client or caller ("mcp_client:claude-desktop").
        risk_level:  Coarse risk classification: "read", "write", "destructive", "unknown".
        description: Human-readable action summary ("Send email to sarah@example.com").
    """

    tool_name: str
    arguments: dict[str, Any]
    source: str
    risk_level: str
    description: str
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Base class ────────────────────────────────────────────────────────────────


class ApprovalBackend(ABC):
    """
    Abstract base for all approval backends.

    Implementors must override `request_approval`.
    The call may block until the user responds (or a timeout is reached).
    """

    @abstractmethod
    async def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        """
        Ask the user (or a policy) whether to allow tool execution.

        Returns ApprovalDecision.APPROVED, .DENIED, or .TIMEOUT.
        Must never raise — catch all exceptions internally and return .DENIED.
        """


# ── Implementations ───────────────────────────────────────────────────────────


class ChannelApprovalBackend(ApprovalBackend):
    """
    Routes approval requests to the user's active messaging channel
    (Telegram, WhatsApp, Discord, etc.) via an injected ask-user callable.

    The callable signature must match:
        async (user_id: str, channel: str, question: str) -> str

    In embedded mode, this callable is typically `agent._ask_user_callback`.
    If no channel is available, the request is denied immediately.

    Args:
        get_ask_callback: Zero-arg callable that returns the current ask-user
                          function, or None if no channel is active. Late-binding
                          allows the channel to come up after the backend is built.
        timeout:          Seconds to wait for a reply before timing out (default 120).
        user_id:          Default user_id for ask-user calls (override at runtime).
        channel:          Default channel name (override at runtime).
    """

    def __init__(
        self,
        get_ask_callback: Callable[[], Callable[[str, str, str], Awaitable[str]] | None],
        timeout: int = 120,
        user_id: str = "mcp_approval",
        channel: str = "mcp",
    ) -> None:
        self._get_ask_callback = get_ask_callback
        self._timeout = timeout
        self._default_user_id = user_id
        self._default_channel = channel

    async def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        ask_cb = self._get_ask_callback()
        if ask_cb is None:
            logger.warning(
                "ChannelApprovalBackend: no active channel — denying '%s'",
                request.tool_name,
            )
            return ApprovalDecision.DENIED

        question = (
            f"🔒 **Approval Required**\n\n"
            f"Tool: `{request.tool_name}`\n"
            f"Source: {request.source}\n"
            f"Risk: {request.risk_level}\n"
            f"Action: {request.description}\n\n"
            f"Reply **yes** / **y** / ✅ to approve, anything else to deny."
        )
        try:
            reply = await asyncio.wait_for(
                ask_cb(self._default_user_id, self._default_channel, question),
                timeout=self._timeout,
            )
            if reply.strip().lower() in ("yes", "y", "✅", "approve", "ok"):
                return ApprovalDecision.APPROVED
            return ApprovalDecision.DENIED
        except TimeoutError:
            logger.warning("ChannelApprovalBackend: approval timed out for '%s'", request.tool_name)
            return ApprovalDecision.TIMEOUT
        except Exception as e:
            logger.exception("ChannelApprovalBackend: error during approval for '%s': %s", request.tool_name, e)
            return ApprovalDecision.DENIED


class PolicyApprovalBackend(ApprovalBackend):
    """
    Auto-approve or deny based on a static policy mapping risk levels to decisions.

    Policy dict maps risk_level strings → "approve" | "deny".
    A "default" key covers any risk level not explicitly listed.

    Example policy (from pincer.toml)::

        [mcp.server.approval_policy]
        read        = "approve"
        write       = "deny"
        destructive = "deny"
        default     = "deny"
    """

    def __init__(self, policy: dict[str, str] | None = None) -> None:
        self._policy: dict[str, str] = policy or {"default": "deny"}

    async def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        action = self._policy.get(request.risk_level) or self._policy.get("default", "deny")
        if action == "approve":
            logger.debug(
                "PolicyApprovalBackend: auto-approved '%s' (risk=%s)",
                request.tool_name,
                request.risk_level,
            )
            return ApprovalDecision.APPROVED
        logger.debug(
            "PolicyApprovalBackend: auto-denied '%s' (risk=%s)",
            request.tool_name,
            request.risk_level,
        )
        return ApprovalDecision.DENIED


class CLIApprovalBackend(ApprovalBackend):
    """
    Interactive terminal prompt — prints request details and waits for user input.

    Suitable for `pincer mcp serve --approval cli` (foreground, interactive).
    Not suitable for headless deployments.
    """

    async def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        print(f"\n🔒 Approval Required: {request.tool_name}")
        print(f"   Source:  {request.source}")
        print(f"   Risk:    {request.risk_level}")
        print(f"   Action:  {request.description}")
        if request.arguments:
            args_preview = str(request.arguments)[:120]
            print(f"   Args:    {args_preview}")
        try:
            # Run blocking input() in executor so we don't block the event loop
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None,
                lambda: input("   Approve? [y/N]: ").strip().lower(),
            )
            if response in ("y", "yes"):
                return ApprovalDecision.APPROVED
            return ApprovalDecision.DENIED
        except (EOFError, KeyboardInterrupt):
            return ApprovalDecision.DENIED
        except Exception as e:
            logger.exception("CLIApprovalBackend: input error: %s", e)
            return ApprovalDecision.DENIED


class WebhookApprovalBackend(ApprovalBackend):
    """
    POST an approval request to a configured URL, then poll for a decision.

    The endpoint receives a JSON body::

        {
            "tool": "tool_name",
            "arguments": {...},
            "source": "mcp_client:...",
            "risk_level": "write",
            "description": "Human-readable action"
        }

    It must respond with a JSON body containing ``{"ticket_id": "..."}`` and
    accept GET requests to ``<url>/<ticket_id>`` returning
    ``{"decision": "approved" | "denied"}``.

    Args:
        webhook_url:   Base URL for the approval service.
        poll_interval: Seconds between polls (default 5).
        timeout:       Total wait seconds before returning TIMEOUT (default 300).
    """

    def __init__(
        self,
        webhook_url: str,
        poll_interval: int = 5,
        timeout: int = 300,
    ) -> None:
        self._url = webhook_url.rstrip("/")
        self._poll_interval = poll_interval
        self._timeout = timeout

    async def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        try:
            import httpx
        except ImportError:
            logger.error("WebhookApprovalBackend requires httpx: uv pip install httpx")
            return ApprovalDecision.DENIED

        payload = {
            "tool": request.tool_name,
            "arguments": request.arguments,
            "source": request.source,
            "risk_level": request.risk_level,
            "description": request.description,
        }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(self._url, json=payload)
                resp.raise_for_status()
                ticket_id = resp.json().get("ticket_id")
                if not ticket_id:
                    logger.warning("WebhookApprovalBackend: no ticket_id in response")
                    return ApprovalDecision.DENIED

                elapsed = 0
                while elapsed < self._timeout:
                    await asyncio.sleep(self._poll_interval)
                    elapsed += self._poll_interval
                    poll = await client.get(f"{self._url}/{ticket_id}")
                    if poll.status_code == 200:
                        decision_str = poll.json().get("decision")
                        if decision_str:
                            try:
                                return ApprovalDecision(decision_str)
                            except ValueError:
                                logger.warning(
                                    "WebhookApprovalBackend: unknown decision '%s'",
                                    decision_str,
                                )
                                return ApprovalDecision.DENIED

                logger.warning(
                    "WebhookApprovalBackend: timed out after %ds for '%s'",
                    self._timeout,
                    request.tool_name,
                )
                return ApprovalDecision.TIMEOUT

        except Exception as e:
            logger.exception("WebhookApprovalBackend: request failed: %s", e)
            return ApprovalDecision.DENIED


# ── Helpers ────────────────────────────────────────────────────────────────────

# Risk classification: maps tool name keywords to risk levels
_DESTRUCTIVE_KEYWORDS = frozenset({
    "delete", "remove", "drop", "clear", "wipe", "reset", "destroy",
    "exec", "shell", "python", "eval",
})
_WRITE_KEYWORDS = frozenset({
    "write", "create", "update", "send", "post", "put", "patch",
    "upload", "publish", "save", "insert",
})
_READ_KEYWORDS = frozenset({
    "read", "get", "list", "search", "find", "check", "fetch",
    "download", "view", "show", "query",
})


def classify_risk(tool_name: str) -> str:
    """
    Classify a tool's risk level based on its name.

    Returns one of: "destructive", "write", "read", "unknown".
    """
    lower = tool_name.lower()
    for kw in _DESTRUCTIVE_KEYWORDS:
        if kw in lower:
            return "destructive"
    for kw in _WRITE_KEYWORDS:
        if kw in lower:
            return "write"
    for kw in _READ_KEYWORDS:
        if kw in lower:
            return "read"
    return "unknown"
