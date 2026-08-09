"""Runtime context for repid actor bodies.

Actors are plain functions invoked by repid's worker loop — they have no
access to the CLI's local variables (result deliverer, proactive agent,
event triggers), so those are stashed here once at process startup.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pincer.scheduler.proactive import ProactiveAgent
    from pincer.scheduler.triggers import EventTriggerManager
    from pincer.tasks.delivery import Deliverer

logger = logging.getLogger(__name__)

_deliverer: Deliverer | None = None
_proactive: ProactiveAgent | None = None
_triggers: EventTriggerManager | None = None


def set_context(deliverer: Deliverer, proactive: ProactiveAgent, triggers: EventTriggerManager) -> None:
    global _deliverer, _proactive, _triggers
    _deliverer = deliverer
    _proactive = proactive
    _triggers = triggers
    logger.info("Task context initialized (deliverer=%s)", type(deliverer).__name__)


def get_deliverer() -> Deliverer:
    if _deliverer is None:
        raise RuntimeError("Task context not initialized — call set_context() at worker startup")
    return _deliverer


def get_proactive() -> ProactiveAgent:
    if _proactive is None:
        raise RuntimeError("Task context not initialized — call set_context() at worker startup")
    return _proactive


def get_triggers() -> EventTriggerManager:
    if _triggers is None:
        raise RuntimeError("Task context not initialized — call set_context() at worker startup")
    return _triggers
