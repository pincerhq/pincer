"""Repid application: broker registration and the actor router.

Repid intentionally has no native scheduler or retry policy (see the
`pincer.tasks` package docstring) — this module only owns the "how it
runs" layer: which broker is active and where actors are registered.
"""

from __future__ import annotations

import logging

from repid import InMemoryServer, Repid, Router

from pincer.config import get_settings
from pincer.config.dsn import is_redis
from pincer.core.cache.keys import KeySpace

logger = logging.getLogger(__name__)

# Logical channel name used by repid for routing/AsyncAPI. The Redis stream
# name is built from it by `KeySpace` (e.g. `production:pincer:stream:tasks`).
TASK_CHANNEL = "tasks"

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
    if is_redis(settings.task_broker_url):
        from repid import RedisServer

        keys = KeySpace.for_tasks(settings)
        server = RedisServer(
            str(settings.task_broker_url),
            stream_name_strategy=keys.stream,
            consumer_group_strategy=keys.stream_group,
            dlq_stream_strategy=keys.stream_dlq,
        )
        app.servers.register_server("default", server, is_default=True)
        logger.info("Task broker registered: redis (prefix=%s)", keys.prefix)
    else:
        app.servers.register_server("default", InMemoryServer(), is_default=True)
        logger.info("Task broker registered: memory")
