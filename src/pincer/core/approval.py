"""Why a tool approval did or didn't go through.

An approval callback used to return a bare ``bool``, so a timeout, a prompt
that could not be shown and a real "no" all reached the LLM as "declined by
the user". Callbacks now return an :class:`ApprovalOutcome`. Plain ``bool``
still works: ``True`` is approved, ``False`` is denied.
"""

from __future__ import annotations

import asyncio
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable


class ApprovalOutcome(StrEnum):
    """Result of asking the user to approve a tool call.

    Only ``APPROVED`` is truthy, so ``if not approved:`` checks written for
    bool callbacks keep refusing everything else.
    """

    APPROVED = "approved"
    DENIED = "denied"
    TIMED_OUT = "timed_out"
    UNAVAILABLE = "unavailable"

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


def approval_tool_message(tool_name: str, outcome: ApprovalOutcome) -> str:
    """Tool-result text the LLM sees when a tool call was not approved.

    Args:
        tool_name: The tool that needed approval.
        outcome: Any outcome except APPROVED.

    Returns:
        A message that names the real reason, so the LLM doesn't invent one.
    """
    return _REFUSAL_MESSAGES[outcome].format(tool=tool_name)
