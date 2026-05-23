"""Inbound message middleware pipeline.

Middleware sits between channel adapters and the agent.  Each stage receives
an IncomingMessage, may mutate it, and returns it.

Usage in cli.py::

    pipeline = build_pipeline(identity)

    async def on_message(msg: IncomingMessage) -> str:
        msg = await pipeline(msg)
        return await agent.handle_message(...)
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pincer.channels.base import IncomingMessage
    from pincer.core.identity import IdentityResolver

logger = logging.getLogger(__name__)

# A middleware stage: receives a message, returns the (possibly mutated) message.
MiddlewareFn = Callable[["IncomingMessage"], Awaitable["IncomingMessage"]]


class IdentityMiddleware:
    """Resolves channel-specific user IDs to a canonical pincer_user_id.

    Adapters emit IncomingMessage with user_id set to the raw channel identifier
    (e.g. a Telegram numeric ID or a WhatsApp LID).  This middleware resolves it
    to the owner defined in PINCER_IDENTITY_MAP, or falls back to the
    channel-scoped form ``{channel}:{user_id}`` so downstream code always sees
    a non-empty, deterministic canonical identity.

    Alternative IDs (msg.alt_user_ids) are tried in order when the primary
    user_id does not match any entry — used by WhatsApp to try the phone-number
    JID alongside the LID.
    """

    def __init__(self, identity: IdentityResolver) -> None:
        self._identity = identity

    async def __call__(self, msg: IncomingMessage) -> IncomingMessage:
        candidates = list(dict.fromkeys(filter(None, [msg.user_id, *msg.alt_user_ids])))

        # Phase 1: look up each candidate without creating anything.
        # This ensures a LID tried first never shadow a phone number that
        # IS already mapped (e.g. via PINCER_IDENTITY_MAP).
        matched_candidate: str | None = None
        owner: str | None = None
        for candidate in candidates:
            try:
                owner = await self._identity.find(msg.channel_type, candidate)
            except Exception:
                logger.debug("Identity find error for %s/%s", msg.channel, candidate, exc_info=True)
                owner = None
            if owner:
                matched_candidate = candidate
                break

        # Phase 2: create a new identity only when no config is set.
        # With a config, startup (seed_from_config + cleanup) is authoritative —
        # unknown IDs must NOT produce new DB rows at runtime.
        if owner is None and msg.user_id and not self._identity.has_config:
            try:
                owner = await self._identity.resolve(msg.channel_type, msg.user_id)
                matched_candidate = msg.user_id
            except Exception:
                logger.debug("Identity resolve error for %s/%s", msg.channel, msg.user_id, exc_info=True)

        if owner:
            msg.pincer_user_id = owner
            logger.debug("Identity resolved: %s/%s → %s", msg.channel, matched_candidate, owner)
            # Back-link any candidate that resolved via an alt so it maps
            # directly on the next message without needing the alt path.
            for candidate in candidates:
                if candidate != matched_candidate:
                    try:
                        await self._identity.link_if_new(owner, msg.channel_type, candidate)
                    except Exception:
                        logger.debug("Failed to back-link %s/%s", msg.channel, candidate, exc_info=True)
        else:
            # Fallback: channel-scoped canonical ID so the agent never sees ""
            msg.pincer_user_id = f"{msg.channel}:{msg.user_id}"

        return msg


def build_pipeline(*stages: MiddlewareFn) -> MiddlewareFn:
    """Compose multiple middleware stages into a single callable."""

    async def pipeline(msg: IncomingMessage) -> IncomingMessage:
        for stage in stages:
            msg = await stage(msg)
        return msg

    return pipeline
