"""ApprovalOutcome: bool compatibility and one tool-result message per outcome."""

from __future__ import annotations

import asyncio

import pytest

from pincer.channels.approval_text import new_answer_code, parse_coded_yes_no
from pincer.core.approval import ApprovalOutcome, PromptQueue, approval_tool_message, as_outcome, await_decision


def test_only_approved_is_truthy():
    assert ApprovalOutcome.APPROVED
    assert not ApprovalOutcome.DENIED
    assert not ApprovalOutcome.TIMED_OUT
    assert not ApprovalOutcome.UNAVAILABLE
    assert not ApprovalOutcome.NO_APPROVER


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


def test_no_approver_message_names_the_missing_chat():
    msg = approval_tool_message("email_send", ApprovalOutcome.NO_APPROVER)
    assert "no Telegram, WhatsApp or Teams chat" in msg
    assert "internal error" not in msg


# ── PromptQueue ──────────────────────────────────────────────────────────────


async def test_prompt_queue_serialises_one_key_and_prunes():
    queue = PromptQueue()
    order: list[str] = []
    release = asyncio.Event()

    async def first():
        async with queue.turn("u1", 1) as ok:
            order.append(f"first:{ok}")
            await release.wait()

    async def second():
        async with queue.turn("u1", 1) as ok:
            order.append(f"second:{ok}")

    tasks = [asyncio.create_task(first()), asyncio.create_task(second())]
    await asyncio.sleep(0.01)
    assert order == ["first:True"]
    release.set()
    await asyncio.gather(*tasks)
    assert order == ["first:True", "second:True"]
    assert len(queue) == 0


async def test_prompt_queue_wait_is_bounded():
    queue = PromptQueue()
    release = asyncio.Event()

    async def hold():
        async with queue.turn("u1", 1):
            await release.wait()

    holder = asyncio.create_task(hold())
    await asyncio.sleep(0)
    async with queue.turn("u1", 0.01) as ok:
        assert ok is False
    release.set()
    await holder
    assert len(queue) == 0


async def test_prompt_queue_cancelled_waiter_leaves_no_lock():
    queue = PromptQueue()
    release = asyncio.Event()
    entered = []

    async def hold():
        async with queue.turn("u1", 1):
            await release.wait()

    async def wait_then_enter():
        async with queue.turn("u1", 5):
            entered.append(True)

    holder = asyncio.create_task(hold())
    await asyncio.sleep(0)
    waiter = asyncio.create_task(wait_then_enter())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    await holder
    assert entered == []
    assert len(queue) == 0


# ── Answer codes ─────────────────────────────────────────────────────────────


def test_answer_code_is_four_digits():
    code = new_answer_code()
    assert len(code) == 4 and code.isdigit()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("yes 4821", True),
        ("4821 yes", True),
        ("Yes, 4821!", True),
        ("no 4821", False),
        ("yes", None),
        ("no", None),
        ("yes 1234", None),
        ("4821", None),
        ("yes 4821 but wait", None),
    ],
)
def test_parse_coded_yes_no(text, expected):
    assert parse_coded_yes_no(text, "4821") is expected
