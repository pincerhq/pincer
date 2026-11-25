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

    assert await router("email_send", {}) is ApprovalOutcome.UNAVAILABLE
    ask.assert_not_awaited()
    is_known.assert_not_awaited()


async def test_unavailable_with_a_warning_when_no_known_user_wrote_yet(caplog):
    router, ask, _ = _router(known=False)
    await router.note_message("telegram:123", "telegram")

    with caplog.at_level(logging.WARNING, logger="pincer.mcp.channel_approval"):
        assert await router("email_send", {}) is ApprovalOutcome.UNAVAILABLE
    ask.assert_not_awaited()
    assert any("web chat can't show MCP approvals" in r.getMessage() for r in caplog.records)


async def test_identity_check_failure_is_not_a_target():
    router, ask, _ = _router(known=AsyncMock(side_effect=RuntimeError("db down")))
    await router.note_message("owner", "telegram")

    assert await router("email_send", {}) is ApprovalOutcome.UNAVAILABLE


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
