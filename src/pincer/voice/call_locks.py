"""One update of a call's transfer state at a time.

/transfer-result, a terminal /status and the engine's settle of an
unconfirmed transfer redirect all decide what happened to the same <Dial>.
Each holds the call's lock while it reads and acts on that call's state, so
none of them acts on evidence another one is about to make stale.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@dataclass
class _CallLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


# CallSid -> its lock; an entry lives only while someone holds or awaits it.
call_update_locks: dict[str, _CallLock] = {}


@asynccontextmanager
async def one_call_update_at_a_time(call_sid: str) -> AsyncIterator[None]:
    entry = call_update_locks.setdefault(call_sid, _CallLock())
    entry.users += 1
    try:
        async with entry.lock:
            yield
    finally:
        entry.users -= 1
        if not entry.users:
            call_update_locks.pop(call_sid, None)
