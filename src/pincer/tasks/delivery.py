"""Result delivery: how a repid actor gets its output back to a user.

Actors (`pincer.tasks.actors`) run wherever the repid worker consuming
`TASK_CHANNEL` happens to be — the in-process worker started by `pincer run`,
or a standalone `pincer run tasks` process. Either way they never touch a
`ChannelRouter` directly, since only the main `pincer run` process owns live,
started channels (channels can't safely be started twice — see
`pincer.tasks.context`). Instead every actor publishes its result through a
`Deliverer`, and a `ResultRelay` running in the main process consumes those
results and hands them to the real router.

The transport (`DeliveryBackend`) is selected the same way as the repid
broker itself (`register_default_server` in `pincer.tasks.app`) — in-process
for `task_broker=memory`, Redis pub/sub for `task_broker=redis` — so the
actor code path is identical regardless of deployment topology. This is a
deliberate simplification: even in the single-process default case, results
now take one extra async hop through the backend instead of a direct call.
Redis pub/sub has no persistence, so if a standalone worker publishes while
no `ResultRelay` is listening, that result is silently dropped — the actor
itself still succeeds (repid acks the message), only delivery is best-effort.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any, Protocol

from redis.asyncio import Redis

from pincer.channels.base import ChannelType

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

TASK_RESULTS_CHANNEL = "pincer_task_results"


class Deliverer(Protocol):
    """What an actor needs to get a result to a user — a `ChannelRouter`, or a `ResultEmitter`."""

    async def send_to_user(
        self,
        pincer_user_id: str,
        text: str,
        prefer: ChannelType | None = None,
        max_active_age_seconds: float | None = None,
    ) -> bool: ...


class DeliveryBackend(Protocol):
    """Pub/sub transport a `ResultEmitter`/`ResultRelay` pair is built on."""

    async def publish(self, envelope: dict[str, Any]) -> None: ...

    def subscribe(self) -> AsyncIterator[dict[str, Any]]: ...

    async def aclose(self) -> None: ...


class InMemoryDeliveryBackend:
    """Single-process backend — a plain queue, since worker and relay share the event loop.

    Unbounded, so publishing never drops a message while the process is
    alive (unlike the Redis backend's fire-and-forget pub/sub) — it only
    exists so the in-process default path exercises the same emitter/relay
    code as the Redis-backed standalone-worker path.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def publish(self, envelope: dict[str, Any]) -> None:
        self._queue.put_nowait(envelope)

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            yield await self._queue.get()

    async def aclose(self) -> None:
        pass


class RedisDeliveryBackend:
    """Cross-process backend — real Redis pub/sub.

    Owns a dedicated `redis.asyncio.Redis` connection rather than reusing
    repid's internal broker connection: repid exposes no accessor for it,
    and a `pubsub()` subscription takes over its own connection anyway.
    """

    def __init__(self, redis_url: str) -> None:
        self._redis: Redis = Redis.from_url(redis_url)

    async def publish(self, envelope: dict[str, Any]) -> None:
        await self._redis.publish(TASK_RESULTS_CHANNEL, json.dumps(envelope))

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(TASK_RESULTS_CHANNEL)
        try:
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                try:
                    yield json.loads(message["data"])
                except (TypeError, ValueError):
                    logger.warning("Dropping malformed task result envelope: %r", message["data"])
        finally:
            await pubsub.unsubscribe(TASK_RESULTS_CHANNEL)
            await pubsub.aclose()  # type: ignore[no-untyped-call]

    async def aclose(self) -> None:
        await self._redis.aclose()


def create_delivery_backend(settings: Any) -> DeliveryBackend:
    """Select the delivery backend matching `settings.task_broker`, mirroring `register_default_server`."""
    if settings.task_broker == "redis":
        return RedisDeliveryBackend(settings.task_broker_url)
    return InMemoryDeliveryBackend()


class ResultEmitter:
    """`Deliverer` used by actors: publishes to a `DeliveryBackend` instead of calling a router."""

    def __init__(self, backend: DeliveryBackend) -> None:
        self._backend = backend

    async def send_to_user(
        self,
        pincer_user_id: str,
        text: str,
        prefer: ChannelType | None = None,
        max_active_age_seconds: float | None = None,
    ) -> bool:
        await self._backend.publish(
            {
                "pincer_user_id": pincer_user_id,
                "text": text,
                "prefer": prefer.value if prefer else None,
                "max_active_age_seconds": max_active_age_seconds,
            }
        )
        # "Handed off to the backend," not "confirmed delivered" — the real
        # delivery outcome is decided later by ResultRelay's router.send_to_user call.
        return True


class ResultRelay:
    """Consumes results published by any `ResultEmitter` and delivers them via the real router."""

    def __init__(self, backend: DeliveryBackend, router: Any) -> None:
        self._backend = backend
        self._router = router
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="pincer-task-result-relay")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._backend.aclose()

    async def _loop(self) -> None:
        async for envelope in self._backend.subscribe():
            try:
                await self._deliver(envelope)
            except Exception:
                logger.exception("Task result relay failed to deliver envelope: %r", envelope)

    async def _deliver(self, envelope: dict[str, Any]) -> None:
        prefer_raw = envelope.get("prefer")
        prefer: ChannelType | None = None
        if prefer_raw:
            try:
                prefer = ChannelType(prefer_raw)
            except ValueError:
                logger.warning("Task result relay: unknown channel %r, ignoring prefer", prefer_raw)

        delivered = await self._router.send_to_user(
            envelope["pincer_user_id"],
            envelope["text"],
            prefer=prefer,
            max_active_age_seconds=envelope.get("max_active_age_seconds"),
        )
        if not delivered:
            logger.error("Task result relay: no reachable channel for user %s", envelope.get("pincer_user_id"))
