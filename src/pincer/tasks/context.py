"""Runtime context for repid actor bodies.

Actors are plain functions invoked by repid's worker loop — they have no
access to the CLI's local variables (channel router, proactive agent,
event triggers), so those are stashed here once at process startup.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pincer.channels.router import ChannelRouter
    from pincer.scheduler.proactive import ProactiveAgent
    from pincer.scheduler.triggers import EventTriggerManager

logger = logging.getLogger(__name__)

_router: ChannelRouter | None = None
_proactive: ProactiveAgent | None = None
_triggers: EventTriggerManager | None = None


def set_context(router: ChannelRouter, proactive: ProactiveAgent, triggers: EventTriggerManager) -> None:
    global _router, _proactive, _triggers
    _router = router
    _proactive = proactive
    _triggers = triggers
    logger.info("Task context initialized (router=%s)", type(router).__name__)


def get_router() -> ChannelRouter:
    if _router is None:
        raise RuntimeError("Task context not initialized — call set_context() at worker startup")
    return _router


def get_proactive() -> ProactiveAgent:
    if _proactive is None:
        raise RuntimeError("Task context not initialized — call set_context() at worker startup")
    return _proactive


def get_triggers() -> EventTriggerManager:
    if _triggers is None:
        raise RuntimeError("Task context not initialized — call set_context() at worker startup")
    return _triggers
