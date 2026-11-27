"""MCPApprovalRouter: MCP-client approvals go to a known user's last chat (PR #423 review)."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from pincer.core.approval import ApprovalOutcome
from pincer.mcp.channel_approval import MCP_REQUESTER, MCPApprovalRouter


def _router(ask=None, known=True):
    ask = ask or AsyncMock(return_value=ApprovalOutcome.APPROVED)
    is_known = AsyncMock(return_value=known) if isinstance(known, bool) else known
    return MCPApprovalRouter(ask=ask, is_known=is_known), ask, is_known


async def test_asks_the_last_known_chat_and_names_the_mcp_client():
    router, ask, _ = _router()
    await router.note_message("owner", "telegram")
    await router.note_message("owner", "whatsapp")

    assert await router("email_send", {"to": "a@b.c"}) is ApprovalOutcome.APPROVED
    ask.assert_awaited_once_with("email_send", {"to": "a@b.c"}, "owner", "whatsapp", requested_by=MCP_REQUESTER)


async def test_a_guest_message_does_not_take_over_the_prompt():
    router, ask, _ = _router(known=AsyncMock(side_effect=lambda uid: uid == "owner"))
    await router.note_message("owner", "telegram")
    await router.note_message("teams:guest-aad-id", "teams")

    await router("email_send", {})
    assert ask.await_args.args[2:4] == ("owner", "telegram")


@pytest.mark.parametrize("channel", ["web", "voice", "cli"])
async def test_channels_that_cannot_show_the_prompt_are_ignored(channel):
    router, ask, is_known = _router()
    await router.note_message("owner", channel)

    assert await router("email_send", {}) is ApprovalOutcome.NO_APPROVER
    ask.assert_not_awaited()
    is_known.assert_not_awaited()


async def test_no_approver_with_a_warning_when_no_known_user_wrote_yet(caplog):
    router, ask, _ = _router(known=False)
    await router.note_message("telegram:123", "telegram")

    with caplog.at_level(logging.WARNING, logger="pincer.mcp.channel_approval"):
        assert await router("email_send", {}) is ApprovalOutcome.NO_APPROVER
    ask.assert_not_awaited()
    assert any("web chat can't show MCP approvals" in r.getMessage() for r in caplog.records)


async def test_identity_check_failure_is_not_a_target():
    router, ask, _ = _router(known=AsyncMock(side_effect=RuntimeError("db down")))
    await router.note_message("owner", "telegram")

    assert await router("email_send", {}) is ApprovalOutcome.NO_APPROVER  # never recorded


async def test_repeat_messages_skip_the_identity_check():
    router, _, is_known = _router()
    await router.note_message("owner", "telegram")
    await router.note_message("owner", "telegram")

    is_known.assert_awaited_once()


async def test_prompt_failure_is_unavailable():
    router, _, _ = _router(ask=AsyncMock(side_effect=RuntimeError("boom")))
    await router.note_message("owner", "telegram")

    assert await router("email_send", {}) is ApprovalOutcome.UNAVAILABLE


async def test_bool_answers_are_read_as_outcomes():
    router, _, _ = _router(ask=AsyncMock(return_value=False))
    await router.note_message("owner", "telegram")

    assert await router("email_send", {}) is ApprovalOutcome.DENIED


# ── PR #423 review round 2 ──────────────────────────────────────────────────


async def test_target_is_rechecked_before_asking_and_dropped_once_deactivated():
    active = {"owner": True}
    router, ask, _ = _router(known=AsyncMock(side_effect=lambda uid: active[uid]))
    await router.note_message("owner", "telegram")
    active["owner"] = False  # `pincer identity deactivate owner`

    assert await router("email_send", {}) is ApprovalOutcome.NO_APPROVER
    ask.assert_not_awaited()
    active["owner"] = True
    assert await router("email_send", {}) is ApprovalOutcome.NO_APPROVER  # dropped, needs a new message


async def test_pinned_approver_ignores_other_known_users():
    router, ask, _ = _router()
    router.approver = "alice"
    await router.note_message("alice", "telegram")
    await router.note_message("bob", "whatsapp")  # an active teammate writes later

    await router("email_send", {})
    assert ask.await_args.args[2:4] == ("alice", "telegram")


async def test_target_recorded_before_the_approver_was_set_is_not_asked():
    router, ask, _ = _router()
    await router.note_message("bob", "telegram")
    router.approver = "alice"

    assert await router("email_send", {}) is ApprovalOutcome.NO_APPROVER
    ask.assert_not_awaited()


async def test_delivery_failure_stays_unavailable_not_no_approver():
    """A known chat whose prompt can't be delivered is not 'no chat' (review: don't invent a cause)."""
    router, _, _ = _router(ask=AsyncMock(return_value=ApprovalOutcome.UNAVAILABLE))
    await router.note_message("owner", "telegram")

    assert await router("email_send", {}) is ApprovalOutcome.UNAVAILABLE


# ── PR #423 review round 3 ──────────────────────────────────────────────────


async def test_identity_check_error_keeps_the_target_and_is_unavailable():
    """A transient DB error is not "no chat": keep the target, report an internal error."""
    checks = iter([True, RuntimeError("connection reset"), True])

    async def is_known(_uid):
        result = next(checks)
        if isinstance(result, Exception):
            raise result
        return result

    router, ask, _ = _router(known=is_known)
    await router.note_message("owner", "telegram")

    assert await router("email_send", {}) is ApprovalOutcome.UNAVAILABLE
    ask.assert_not_awaited()
    assert await router("email_send", {}) is ApprovalOutcome.APPROVED  # target kept
    assert ask.await_args.args[2:4] == ("owner", "telegram")


async def test_new_target_recorded_during_the_check_is_kept():
    router, ask, _ = _router()
    await router.note_message("alice", "telegram")

    async def deactivated_while_bob_writes(uid):
        if uid == "alice":
            router._target = ("bob", "telegram")  # bob's message lands during the check
            return False
        return True

    router._is_known = deactivated_while_bob_writes
    assert await router("email_send", {}) is ApprovalOutcome.NO_APPROVER
    assert router._target == ("bob", "telegram")


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ("none", "no known user has messaged on Telegram, WhatsApp or Teams yet"),
        ("pinned", "approver 'alice' has not messaged on Telegram, WhatsApp or Teams yet"),
        ("deactivated", "recorded approver bob is no longer active"),
        ("not_pinned", "recorded user bob is not the configured approver"),
    ],
)
async def test_no_approver_log_names_the_reason(caplog, setup, expected):
    active = {"bob": True}
    router, _, _ = _router(known=AsyncMock(side_effect=lambda uid: active.get(uid, False)))
    if setup == "pinned":
        router.approver = "alice"
    if setup in ("deactivated", "not_pinned"):
        await router.note_message("bob", "telegram")
    if setup == "deactivated":
        active["bob"] = False
    if setup == "not_pinned":
        router.approver = "alice"

    with caplog.at_level(logging.WARNING, logger="pincer.mcp.channel_approval"):
        assert await router("email_send", {}) is ApprovalOutcome.NO_APPROVER
    assert any(expected in r.getMessage() for r in caplog.records)


async def test_check_approver_warns_for_an_unknown_approver(caplog):
    router, _, _ = _router(known=False)
    router.approver = "alcie"

    with caplog.at_level(logging.WARNING, logger="pincer.mcp.channel_approval"):
        await router.check_approver()
    assert any("approver 'alcie' is not an active identity" in r.getMessage() for r in caplog.records)


async def test_check_approver_is_quiet_for_an_active_approver_or_a_failed_check(caplog):
    for known in (True, AsyncMock(side_effect=RuntimeError("db down"))):
        router, _, _ = _router(known=known)
        router.approver = "alice"
        with caplog.at_level(logging.WARNING, logger="pincer.mcp.channel_approval"):
            await router.check_approver()
    assert not any("not an active identity" in r.getMessage() for r in caplog.records)
