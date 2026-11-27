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

    async def note_message(self, user_id: str, channel: str) -> None:
        """Record that *user_id* just messaged on *channel*; guests, web chat and non-approvers are ignored."""
        if channel not in PROMPT_CHANNELS or self._target == (user_id, channel):
            return
        if self.approver is not None and user_id != self.approver:
            return
        if await self._known(user_id):
            self._target = (user_id, channel)

    async def _known(self, user_id: str) -> bool:
        try:
            return await self._is_known(user_id)
        except Exception:
            logger.debug("Identity check failed for %s; not an MCP approval target", user_id, exc_info=True)
            return False

    async def __call__(self, tool_name: str, arguments: dict[str, Any]) -> ApprovalOutcome:
        """Ask for approval of *tool_name*.

        NO_APPROVER when there is nobody to ask: no known user's chat yet, or
        the one recorded is no longer active or not the configured approver.
        """
        target = self._target
        if target is not None and (
            (self.approver is not None and target[0] != self.approver) or not await self._known(target[0])
        ):
            # Deactivated since, or recorded before `approver` was set.
            self._target = target = None
        if target is None:
            logger.warning(
                "MCP approval for %s: %s has messaged on Telegram, WhatsApp or Teams yet "
                "(web chat can't show MCP approvals); nobody to ask",
                tool_name,
                f"approver {self.approver!r}" if self.approver else "no known user",
            )
            return ApprovalOutcome.NO_APPROVER
        user_id, channel = target
        try:
            return as_outcome(await self._ask(tool_name, arguments, user_id, channel, requested_by=MCP_REQUESTER))
        except Exception:
            logger.exception("Approval prompt failed for MCP tool '%s'", tool_name)
            return ApprovalOutcome.UNAVAILABLE
