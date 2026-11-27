"""Why a tool approval did or didn't go through.

An approval callback used to return a bare ``bool``, so a timeout, a prompt
that could not be shown and a real "no" all reached the LLM as "declined by
the user". Callbacks now return an :class:`ApprovalOutcome`. Plain ``bool``
still works: ``True`` is approved, ``False`` is denied.
"""

from __future__ import annotations

import asyncio
import contextlib
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Hashable


class ApprovalOutcome(StrEnum):
    """Result of asking the user to approve a tool call.

    Only ``APPROVED`` is truthy, so ``if not approved:`` checks written for
    bool callbacks keep refusing everything else.
    """

    APPROVED = "approved"
    DENIED = "denied"
    TIMED_OUT = "timed_out"
    UNAVAILABLE = "unavailable"
    # Nobody could be asked: no active approver chat is known (MCP-client calls).
    NO_APPROVER = "no_approver"
    # Never shown: an earlier prompt to the same user stayed open too long.
    BUSY = "busy"

    def __bool__(self) -> bool:
        return self is ApprovalOutcome.APPROVED


# Tool-result text per refusal; {tool} is the tool name.
_REFUSAL_MESSAGES = {
    ApprovalOutcome.DENIED: "Action '{tool}' was declined by the user. Do not retry unless they ask.",
    ApprovalOutcome.TIMED_OUT: (
        "The approval request for '{tool}' expired without an answer. Tell the user and offer to try again."
    ),
    ApprovalOutcome.UNAVAILABLE: (
        "Approval for '{tool}' could not be requested (internal error). Tell the user; do not guess a cause."
    ),
    ApprovalOutcome.NO_APPROVER: (
        "Approval for '{tool}' could not be requested: there is no active approver chat on Telegram, WhatsApp "
        "or Teams (web chat can't show it). Tell the user; do not guess a cause."
    ),
    ApprovalOutcome.BUSY: (
        "Approval for '{tool}' was not requested: the user still had another approval prompt open, so this one "
        "was never shown. Tell the user and offer to try again once they have answered it."
    ),
}


def as_outcome(result: bool | ApprovalOutcome) -> ApprovalOutcome:
    """Read an approval callback's result.

    Args:
        result: What the callback returned. A plain bool means approved or denied.

    Returns:
        The matching outcome.
    """
    if isinstance(result, ApprovalOutcome):
        return result
    return ApprovalOutcome.APPROVED if result else ApprovalOutcome.DENIED


async def await_decision(decision: Awaitable[bool], timeout: float) -> ApprovalOutcome:
    """Wait for the user's yes/no answer to an approval prompt.

    Args:
        decision: Resolves to True (approve) or False (deny) when the user answers.
        timeout: Seconds to wait before giving up.

    Returns:
        APPROVED or DENIED from the answer, or TIMED_OUT if none came in time.
    """
    try:
        return as_outcome(await asyncio.wait_for(decision, timeout=timeout))
    except TimeoutError:
        return ApprovalOutcome.TIMED_OUT


# How long a prompt waits for an earlier one to the same user before giving
# up (BUSY). An agent prompt outlasts one whole earlier prompt (120 s answer
# window), so it gets its turn; an MCP client's is short enough that the
# caller (often ~60 s) is still there.
AGENT_PROMPT_QUEUE_TIMEOUT = 150.0
MCP_PROMPT_QUEUE_TIMEOUT = 30.0


def prompt_queue_timeout(requested_by: str | None) -> float:
    """Queue wait for a prompt; *requested_by* is set for an MCP client's prompt."""
    return MCP_PROMPT_QUEUE_TIMEOUT if requested_by else AGENT_PROMPT_QUEUE_TIMEOUT


class PromptQueue:
    """One open prompt per key (a user) at a time; later prompts wait their turn, but not forever.

    For channels where an answer can't say which prompt it is for (a typed
    "yes"). A key's lock is dropped once nobody holds or waits for it.
    """

    def __init__(self) -> None:
        self._locks: dict[Hashable, asyncio.Lock] = {}
        self._users: dict[Hashable, int] = {}

    @contextlib.asynccontextmanager
    async def turn(self, key: Hashable, timeout: float) -> AsyncIterator[bool]:
        """Wait up to *timeout* seconds for *key*'s turn.

        Yields:
            True once it is this caller's turn, False if the wait timed out.
        """
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._users[key] = self._users.get(key, 0) + 1
        try:
            try:
                await asyncio.wait_for(lock.acquire(), timeout=timeout)
                acquired = True
            except TimeoutError:
                acquired = False
            if not acquired:
                yield False
                return
            try:
                yield True
            finally:
                lock.release()
        finally:
            self._users[key] -= 1
            if not self._users[key]:
                del self._users[key]
                del self._locks[key]

    def __len__(self) -> int:
        return len(self._locks)


def approval_tool_message(tool_name: str, outcome: ApprovalOutcome) -> str:
    """Tool-result text the LLM sees when a tool call was not approved.

    Args:
        tool_name: The tool that needed approval.
        outcome: Any outcome except APPROVED.

    Returns:
        A message that names the real reason, so the LLM doesn't invent one.
    """
    return _REFUSAL_MESSAGES[outcome].format(tool=tool_name)
