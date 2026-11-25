"""Approval prompts for tools an external MCP client calls.

An MCP call has no chat of its own, so the prompt goes to the chat where a
known user last talked to Pincer. Only Telegram, WhatsApp and Teams count:
web chat can show a card only inside its own streaming request, and a guest
must never get to approve what the owner's MCP client asked for.
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

    def __init__(self, ask: ChannelApproval, is_known: Callable[[str], Awaitable[bool]]) -> None:
        """
        Args:
            ask: Shows the prompt on a channel and waits for the answer.
            is_known: True for a known, active identity (not a guest).
        """
        self._ask = ask
        self._is_known = is_known
        self._target: tuple[str, str] | None = None

    async def note_message(self, user_id: str, channel: str) -> None:
        """Record that *user_id* just messaged on *channel*; guests and web chat are ignored."""
        if channel not in PROMPT_CHANNELS or self._target == (user_id, channel):
            return
        try:
            known = await self._is_known(user_id)
        except Exception:
            logger.debug("Identity check failed for %s; not an MCP approval target", user_id, exc_info=True)
            return
        if known:
            self._target = (user_id, channel)

    async def __call__(self, tool_name: str, arguments: dict[str, Any]) -> ApprovalOutcome:
        """Ask for approval of *tool_name*. UNAVAILABLE when no chat can show the prompt."""
        if self._target is None:
            logger.warning(
                "MCP approval for %s: no known user has messaged on Telegram, WhatsApp or Teams yet "
                "(web chat can't show MCP approvals); approval unavailable",
                tool_name,
            )
            return ApprovalOutcome.UNAVAILABLE
        user_id, channel = self._target
        try:
            return as_outcome(await self._ask(tool_name, arguments, user_id, channel, requested_by=MCP_REQUESTER))
        except Exception:
            logger.exception("Approval prompt failed for MCP tool '%s'", tool_name)
            return ApprovalOutcome.UNAVAILABLE
