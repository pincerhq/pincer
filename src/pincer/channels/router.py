"""
Cross-channel message router for proactive outbound delivery.

Used by scheduler/proactive.py and scheduler/triggers.py to deliver
messages to users without knowing which channel they prefer.

Does NOT replace the existing agent message flow. The agent still
receives messages via channel-specific handlers (telegram.py, whatsapp.py).
This router is ONLY for proactive outbound delivery.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pincer.channels.base import BaseChannel, ChannelType
    from pincer.core.identity import IdentityResolver

logger = logging.getLogger(__name__)


class ChannelRouter:
    """Routes proactive outbound messages to the correct channel."""

    def __init__(self, identity: IdentityResolver) -> None:
        self._channels: dict[ChannelType, BaseChannel] = {}
        self._identity = identity

    @property
    def channels(self) -> dict[ChannelType, BaseChannel]:
        return self._channels

    def register(self, channel_type: ChannelType, channel_instance: BaseChannel) -> None:
        self._channels[channel_type] = channel_instance
        logger.info("Router registered channel: %s", channel_type.value)

    async def rebuild_identity_map(self) -> None:
        """Seed/cleanup the identity map, then unblock is_guest() for all channels.

        mark_seeding_complete() always runs, even on failure, so a seeding error
        degrades to "treated as complete despite partial data" (logged loudly)
        rather than hanging every channel's guest check.
        """
        try:
            await self._identity.seed_from_config(self._channels)
            await self._identity.cleanup(self._channels)
        except Exception:
            logger.exception("rebuild_identity_map: seeding/cleanup failed — guest gate unblocking anyway")
        finally:
            self._identity.mark_seeding_complete()

    async def send(self, channel_type: ChannelType, chat_id: str, text: str) -> bool:
        """Send a message via the specified channel. Returns True on success."""
        channel = self._channels.get(channel_type)
        if channel is None:
            logger.warning("Router: channel %s not registered", channel_type.value)
            return False
        try:
            await channel.send(chat_id, text)
            return True
        except Exception as e:
            logger.error("Router send failed (%s): %s", channel_type.value, e)
            return False

    async def send_to_user(
        self,
        pincer_user_id: str,
        text: str,
        prefer: ChannelType | None = None,
        max_active_age_seconds: float | None = None,
    ) -> bool:
        """
        Send to a user on their preferred channel.
        Falls back to other channels if preferred fails.

        `max_active_age_seconds` bounds how stale the user's tracked
        "active channel" may be before it's ignored in favor of the durable
        `preferred_channel` — see `IdentityResolver.get_preferred_channel`.
        Default `None` preserves today's behavior (no staleness check).
        """
        if prefer:
            all_channels = await self._identity.get_all_channels(pincer_user_id)
            if prefer in all_channels:
                success = await self.send(prefer, all_channels[prefer], text)
                if success:
                    return True

        try:
            channel_type, chat_id = await self._identity.get_preferred_channel(
                pincer_user_id,
                max_active_age_seconds=max_active_age_seconds,
            )
            return await self.send(channel_type, chat_id, text)
        except ValueError:
            logger.error("Router: no channels for user %s", pincer_user_id)
            return False
