"""Approval prompts for tools an external MCP client calls.

An MCP call has no chat of its own, so the prompt goes to the chat where a
known user last talked to Pincer. Only Telegram, WhatsApp and Teams count:
web chat can show a card only inside its own streaming request, and a guest
must never get to approve what the owner's MCP client asked for.

With ``approver`` set (``[mcp.server] approver`` in ``pincer.toml``), only
that identity is asked. Without it, the last known identity to write wins,
which in a shared deployment may be a teammate rather than the MCP client's
owner.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol

from pincer.core.approval import ApprovalOutcome, as_outcome

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

PROMPT_CHANNELS = frozenset({"telegram", "whatsapp", "teams"})
MCP_REQUESTER = "an MCP client"


class ChannelApproval(Protocol):
    """Shows an approval prompt on a chat channel (see `cli/run.py` `_channel_approval`)."""

    def __call__(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        user_id: str,
        channel: str,
        *,
        requested_by: str | None = None,
    ) -> Awaitable[bool | ApprovalOutcome]: ...


class MCPApprovalRouter:
    """Sends MCP-client approval prompts to a known user's most recent chat."""

    def __init__(
        self,
        ask: ChannelApproval,
        is_known: Callable[[str], Awaitable[bool]],
        approver: str | None = None,
    ) -> None:
        """
        Args:
            ask: Shows the prompt on a channel and waits for the answer.
            is_known: True for a known, active identity (not a guest).
            approver: The only pincer_user_id to ask, or None for the last known one to write.
        """
        self._ask = ask
        self._is_known = is_known
        self.approver = approver
        self._target: tuple[str, str] | None = None

    async def check_approver(self) -> None:
        """Warn (at startup) when the configured `approver` is not an active identity."""
        if self.approver and await self._known(self.approver) is False:
            logger.warning(
                "[mcp.server] approver %r is not an active identity; MCP tool calls that need approval "
                "will be refused until it is",
                self.approver,
            )

    async def note_message(self, user_id: str, channel: str) -> None:
        """Record that *user_id* just messaged on *channel*; guests, web chat and non-approvers are ignored."""
        if channel not in PROMPT_CHANNELS or self._target == (user_id, channel):
            return
        if self.approver is not None and user_id != self.approver:
            return
        if await self._known(user_id):
            self._target = (user_id, channel)

    async def _known(self, user_id: str) -> bool | None:
        """True for a known, active identity, False if not, None if the check failed."""
        try:
            return await self._is_known(user_id)
        except Exception:
            logger.warning("Identity check failed for %s (MCP approval)", user_id, exc_info=True)
            return None

    def _drop(self, target: tuple[str, str]) -> None:
        # Only if unchanged: a message may have recorded a new target meanwhile.
        if self._target == target:
            self._target = None

    async def __call__(self, tool_name: str, arguments: dict[str, Any]) -> ApprovalOutcome:
        """Ask for approval of *tool_name*.

        NO_APPROVER when there is nobody to ask: no known user's chat yet, or
        the one recorded is no longer active or not the configured approver.
        UNAVAILABLE when the identity check or the prompt itself failed.
        """
        target = self._target
        if target is None:
            reason = (
                f"approver {self.approver!r} has no recorded chat on Telegram, WhatsApp or Teams "
                "(not seen since start, or not active)"
                if self.approver
                else "no known user has messaged on Telegram, WhatsApp or Teams yet"
            )
            return self._no_approver(tool_name, reason)
        user_id, channel = target
        if self.approver is not None and user_id != self.approver:
            # Recorded before `approver` was set.
            self._drop(target)
            return self._no_approver(tool_name, f"recorded user {user_id} is not the configured approver")
        known = await self._known(user_id)
        if known is None:
            return ApprovalOutcome.UNAVAILABLE
        if not known:
            self._drop(target)
            return self._no_approver(tool_name, f"recorded approver {user_id} is no longer active")
        try:
            return as_outcome(await self._ask(tool_name, arguments, user_id, channel, requested_by=MCP_REQUESTER))
        except Exception:
            logger.exception("Approval prompt failed for MCP tool '%s'", tool_name)
            return ApprovalOutcome.UNAVAILABLE

    @staticmethod
    def _no_approver(tool_name: str, reason: str) -> ApprovalOutcome:
        logger.warning("MCP approval for %s: %s (web chat can't show MCP approvals); nobody to ask", tool_name, reason)
        return ApprovalOutcome.NO_APPROVER
