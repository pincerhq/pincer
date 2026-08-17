"""Repid application: broker registration and the actor router.

Repid intentionally has no native scheduler or retry policy (see the
`pincer.tasks` package docstring) — this module only owns the "how it
runs" layer: which broker is active and where actors are registered.
"""

from __future__ import annotations

import logging

from repid import InMemoryServer, Repid, Router

from pincer.config import get_settings

logger = logging.getLogger(__name__)

TASK_CHANNEL = "pincer_tasks"

app = Repid(title="pincer-tasks")
router = Router(channel=TASK_CHANNEL)
app.include_router(router)


def register_default_server() -> None:
    """Register the configured broker as the default server.

    Call once at process startup, before opening the server connection —
    either from `pincer run`'s in-process worker or a standalone
    `pincer tasks worker` process.
    """
    settings = get_settings()
    if settings.task_broker == "redis":
        from repid import RedisServer

        app.servers.register_server("default", RedisServer(settings.task_broker_url), is_default=True)
        logger.info("Task broker registered: redis")
    else:
        app.servers.register_server("default", InMemoryServer(), is_default=True)
        logger.info("Task broker registered: memory")
