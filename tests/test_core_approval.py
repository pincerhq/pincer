"""ApprovalOutcome: bool compatibility and one tool-result message per outcome."""

from __future__ import annotations

import asyncio

import pytest

from pincer.core.approval import ApprovalOutcome, approval_tool_message, as_outcome, await_decision


def test_only_approved_is_truthy():
    assert ApprovalOutcome.APPROVED
    assert not ApprovalOutcome.DENIED
    assert not ApprovalOutcome.TIMED_OUT
    assert not ApprovalOutcome.UNAVAILABLE


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (True, ApprovalOutcome.APPROVED),
        (False, ApprovalOutcome.DENIED),
        (ApprovalOutcome.TIMED_OUT, ApprovalOutcome.TIMED_OUT),
        (ApprovalOutcome.UNAVAILABLE, ApprovalOutcome.UNAVAILABLE),
    ],
)
def test_as_outcome_keeps_bool_callbacks_working(result, expected):
    assert as_outcome(result) is expected


def test_denied_message_keeps_declined_by_the_user():
    msg = approval_tool_message("email_send", ApprovalOutcome.DENIED)
    assert "Action 'email_send' was declined by the user." in msg
    assert "Do not retry" in msg


def test_timed_out_message_offers_retry():
    msg = approval_tool_message("email_send", ApprovalOutcome.TIMED_OUT)
    assert "declined" not in msg
    assert "expired" in msg
    assert "try again" in msg


def test_unavailable_message_says_internal_error():
    msg = approval_tool_message("email_send", ApprovalOutcome.UNAVAILABLE)
    assert "declined" not in msg
    assert "could not be requested" in msg
    assert "do not guess a cause" in msg


@pytest.mark.parametrize(("answer", "expected"), [(True, ApprovalOutcome.APPROVED), (False, ApprovalOutcome.DENIED)])
async def test_await_decision_reads_the_answer(answer, expected):
    fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    fut.set_result(answer)
    assert await await_decision(fut, 1) is expected


async def test_await_decision_times_out():
    fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    assert await await_decision(fut, 0.01) is ApprovalOutcome.TIMED_OUT
