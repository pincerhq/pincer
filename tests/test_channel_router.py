"""Tests for ChannelRouter — proactive message routing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pincer.channels.base import ChannelType
from pincer.channels.router import ChannelRouter


def _router_with_channel(channel_type: ChannelType, send_result: bool = True) -> tuple[ChannelRouter, MagicMock]:
    identity = MagicMock()
    router = ChannelRouter(identity)
    channel = MagicMock()
    channel.send = AsyncMock(return_value=None) if send_result else AsyncMock(side_effect=RuntimeError("fail"))
    router.register(channel_type, channel)
    return router, channel


@pytest.mark.asyncio
class TestSendToUser:
    async def test_forwards_max_active_age_seconds_to_get_preferred_channel(self) -> None:
        router, _channel = _router_with_channel(ChannelType.TELEGRAM)
        router._identity.get_preferred_channel = AsyncMock(return_value=(ChannelType.TELEGRAM, "12345"))

        await router.send_to_user("usr_abc", "hello", max_active_age_seconds=1800)

        router._identity.get_preferred_channel.assert_awaited_once_with(
            "usr_abc", max_active_age_seconds=1800
        )

    async def test_default_max_active_age_is_none(self) -> None:
        router, _channel = _router_with_channel(ChannelType.TELEGRAM)
        router._identity.get_preferred_channel = AsyncMock(return_value=(ChannelType.TELEGRAM, "12345"))

        await router.send_to_user("usr_abc", "hello")

        router._identity.get_preferred_channel.assert_awaited_once_with(
            "usr_abc", max_active_age_seconds=None
        )

    async def test_no_channels_returns_false(self) -> None:
        router, _channel = _router_with_channel(ChannelType.TELEGRAM)
        router._identity.get_preferred_channel = AsyncMock(side_effect=ValueError("no channels"))

        sent = await router.send_to_user("usr_abc", "hello")
        assert sent is False
