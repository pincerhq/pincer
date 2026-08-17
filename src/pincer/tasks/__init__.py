"""
Pincer background task execution (repid).

repid (https://github.com/aleksul/repid) is deliberately not
batteries-included: it has no native cron/periodic scheduling and no
built-in retry policy. So this package splits responsibilities:

- app.py: the Repid application instance, default Router, broker
  registration (in-memory by default, Redis when configured)
- context.py: runtime context (channel router, proactive agent, event
  triggers) actor bodies need but don't otherwise have access to
- actors.py: @router.actor definitions — durable, ack'd, retried
  (via tenacity) execution of scheduled and on-request work
- dispatch.py: ScheduleDispatcher — the SQLite cron poll loop that
  decides *when* (still hand-rolled, since repid doesn't provide this)
  and enqueues into repid for execution
"""

from pincer.tasks import actors  # noqa: F401 — registers actors on `router`
from pincer.tasks.app import TASK_CHANNEL, app, register_default_server, router
from pincer.tasks.dispatch import ScheduleDispatcher

__all__ = [
    "TASK_CHANNEL",
    "ScheduleDispatcher",
    "app",
    "register_default_server",
    "router",
]
