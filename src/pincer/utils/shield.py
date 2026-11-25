"""`asyncio.shield` for writes that must finish even when the turn awaiting them is cancelled.

`asyncio.shield` keeps no strong reference to the task it runs, and the event loop holds tasks only
weakly: once the awaiting turn is cancelled, nothing else would keep a half-done write from being
garbage-collected mid-way, and nobody would see it fail. `shielded` keeps the task until it finishes
and logs its failure itself.

This is a leaf module: it imports nothing from pincer, so any layer can use it.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import logging
    from collections.abc import Awaitable

#: Shielded tasks still running.
SHIELDED_TASKS: set[asyncio.Task[Any]] = set()


def shielded[T](coro: Awaitable[T], *, logger: logging.Logger, failure: str) -> asyncio.Future[T]:
    """Run `coro` as a task kept alive until it finishes, behind `asyncio.shield`.

    Args:
        coro: The write to finish whatever happens to its caller.
        logger: Where a failure is logged, once the caller may no longer be there to see it.
        failure: The message a failure is logged with.

    Returns:
        The shielded future: cancelling it leaves the task running.
    """
    task = asyncio.ensure_future(coro)
    SHIELDED_TASKS.add(task)
    task.add_done_callback(SHIELDED_TASKS.discard)

    def log_failure(done: asyncio.Future[Any]) -> None:
        if not done.cancelled() and done.exception() is not None:
            logger.error(failure, exc_info=done.exception())

    task.add_done_callback(log_failure)
    return asyncio.shield(task)
