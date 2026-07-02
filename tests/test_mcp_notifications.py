"""Tests for pincer.mcp.notifications — routing MCP server notifications into chat."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pincer.mcp.notifications import MAX_ACTIVE_CHANNEL_AGE_SECONDS, create_auth_notification_handler


def _params(logger: str | None, data: object) -> MagicMock:
    params = MagicMock()
    params.logger = logger
    params.data = data
    return params


async def test_routes_matching_notification_to_user() -> None:
    router = MagicMock()
    router.send_to_user = AsyncMock(return_value=True)
    handler = create_auth_notification_handler(router)

    await handler(
        _params(
            "some.server.logger",
            {"msg": "Background task complete.", "extra": {"identity": "usr_abc"}},
        )
    )

    router.send_to_user.assert_awaited_once_with(
        "usr_abc", "Background task complete.", max_active_age_seconds=MAX_ACTIVE_CHANNEL_AGE_SECONDS
    )


async def test_uses_30_minute_staleness_threshold() -> None:
    assert MAX_ACTIVE_CHANNEL_AGE_SECONDS == 30 * 60


async def test_drops_notification_with_no_identity_in_extra() -> None:
    router = MagicMock()
    router.send_to_user = AsyncMock(return_value=True)
    handler = create_auth_notification_handler(router)

    await handler(_params("some.server.logger", {"msg": "hi", "extra": {}}))

    router.send_to_user.assert_not_awaited()


async def test_drops_notification_with_no_extra_at_all() -> None:
    router = MagicMock()
    router.send_to_user = AsyncMock(return_value=True)
    handler = create_auth_notification_handler(router)

    await handler(_params("some.server.logger", {"msg": "hi"}))

    router.send_to_user.assert_not_awaited()


async def test_handles_non_dict_data_gracefully() -> None:
    router = MagicMock()
    router.send_to_user = AsyncMock(return_value=True)
    handler = create_auth_notification_handler(router)

    await handler(_params("some.server.logger", "a plain string payload"))

    router.send_to_user.assert_not_awaited()


async def test_falls_back_to_str_data_when_msg_missing() -> None:
    router = MagicMock()
    router.send_to_user = AsyncMock(return_value=True)
    handler = create_auth_notification_handler(router)

    await handler(_params("some.server.logger", {"extra": {"identity": "usr_abc"}}))

    router.send_to_user.assert_awaited_once()
    args, _ = router.send_to_user.call_args
    assert args[0] == "usr_abc"


async def test_logs_warning_when_send_fails(caplog: pytest.LogCaptureFixture) -> None:
    router = MagicMock()
    router.send_to_user = AsyncMock(return_value=False)
    handler = create_auth_notification_handler(router)

    with caplog.at_level("WARNING"):
        await handler(_params("some.server.logger", {"msg": "hi", "extra": {"identity": "usr_abc"}}))

    assert any("usr_abc" in record.message for record in caplog.records)
