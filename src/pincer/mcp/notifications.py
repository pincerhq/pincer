"""
Routes server-initiated MCP notifications into user-facing chat.

Some MCP servers do work that outlives the tool call that triggered it — e.g.
ms365-mcp's per-identity device-code auth flow: a tool call for a
not-yet-signed-in identity returns immediately with sign-in instructions, and
the actual sign-in completes (or fails) minutes later, well after that tool
call's response has already been sent. The server relays that outcome as an
MCP logging notification (`ctx.info`/`ctx.error` on the fastmcp side) instead
of a tool result. `MCPClientSession.set_notification_handler()` is the
generic hook this module plugs into.

Convention: a server opts into this by tagging its notification with a
`logger` name and putting an `identity` key in the `extra` mapping it logs
with (fastmcp wraps that into `params.data["extra"]`) — see ms365-mcp's
`identity_session.py::_notify`. `identity` is expected to be the
`pincer_user_id` that was passed as `_meta.identity` on the original tool
call (`MCPClientSession.call_tool`), since Pincer is the one who put it there
in the first place (see `pincer/mcp/bridge.py`).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mcp.types import LoggingMessageNotificationParams

    from pincer.channels.router import ChannelRouter

logger = logging.getLogger("pincer.mcp.notifications")

# If the user's tracked active channel is older than this by the time the
# notification is actually processed (the triggering device-code flow can run
# for many minutes in the background), it's more likely stale than current —
# fall back to the durable preferred_channel instead. See
# IdentityResolver.get_preferred_channel's `max_active_age_seconds`.
MAX_ACTIVE_CHANNEL_AGE_SECONDS = 30 * 60


def create_auth_notification_handler(
    router: ChannelRouter,
) -> Callable[[LoggingMessageNotificationParams], Awaitable[None]]:
    """Build a notification handler that relays mcp notifications outcomes to chat.

    Pass the result to `MCPClientSession.set_notification_handler()` (or
    `MCPClientManager.set_notification_handler()` to cover every connected
    server at once — non-matching loggers are ignored, so it's safe to
    install broadly).
    """

    async def handle(params: LoggingMessageNotificationParams) -> None:
        data = params.data if isinstance(params.data, dict) else {}
        extra = data.get("extra")
        identity = extra.get("identity") if isinstance(extra, dict) else None
        msg = data.get("msg")
        message = msg if isinstance(msg, str) else str(params.data)

        if not identity:
            logger.debug("Got notification with no identity in extra — dropping: %s", params.data)
            return

        sent = await router.send_to_user(identity, message, max_active_age_seconds=MAX_ACTIVE_CHANNEL_AGE_SECONDS)
        if not sent:
            logger.warning("Could not deliver notification to user_id=%s", identity)

    return handle
