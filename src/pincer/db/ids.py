"""Row identifiers.

Every id Pincer mints for a row of its own is a UUIDv7: a 48-bit millisecond
timestamp followed by a counter and random bits. Time-ordered, so it keeps the
one useful property the autoincrement keys had — rows sort by when they were
written — without a sequence, a round trip to read the id back, or a number
that leaks how many rows there are.

Identifiers that come from somewhere else are not minted here and are not
UUIDs: Twilio call SIDs, phone numbers, W3C trace and span ids, the SHA-1
idempotency key for a provider's retried callbacks, and the derived
`{channel}:{user}` session key.
"""

from __future__ import annotations

import os
import uuid

#: RFC 9562 §5.7, laid out the way CPython's `uuid.uuid7` lays it out:
#:
#:     48 bits unix_ts_ms | 4 version | 12 counter_hi | 2 variant | 30 counter_lo | 32 random
#:
#: The counter is 42 bits split either side of the variant, and its MSB stays
#: 0 so it can be incremented without carrying into the variant bits.
_VERSION_AND_VARIANT = (0x7 << 76) | (0b10 << 62)
_MAX_COUNTER = 0x3FF_FFFF_FFFF
_MAX_MS = 0xFFFF_FFFF_FFFF


def new_id() -> str:
    """A fresh row id, canonical and lowercase."""
    return str(uuid.uuid7())


def uuid7_at(ms: int, counter: int = 0) -> str:
    """A UUIDv7 carrying `ms`, ordered within that millisecond by `counter`.

    For backfilling rows that already exist: their id has to say when the row
    was written, not when the migration ran. The shape is identical to
    `new_id()`'s — same field layout, same random tail — so nothing downstream
    can tell a backfilled id from a minted one.
    """
    if not 0 <= ms <= _MAX_MS:
        raise ValueError(f"timestamp {ms} does not fit in 48 bits")
    if not 0 <= counter <= _MAX_COUNTER:
        raise ValueError(f"counter {counter} does not fit in 42 bits")

    value = ms << 80
    value |= (counter >> 30 & 0x0FFF) << 64
    value |= (counter & 0x3FFF_FFFF) << 32
    value |= int.from_bytes(os.urandom(4))
    return str(uuid.UUID(int=value | _VERSION_AND_VARIANT))


class Uuid7Sequence:
    """Ids for rows that already exist, in the order they are fed in.

    Strictly increasing *by construction*: a timestamp that ties with the
    previous row, runs backwards, or is missing entirely reuses the last
    millisecond and takes the next counter value instead. So feeding a table's
    rows in their original order is enough to preserve that order, and the
    four nullable creation-time columns need no sentinel date.

    This is what lets a backfill keep the ordering that `call_transcripts` and
    `inbound_messages` get today from an autoincrement tiebreaker.
    """

    def __init__(self) -> None:
        self._ms = 0
        self._counter = 0

    def next(self, ms: int | None) -> str:
        """The next id, at `ms` if that is still moving forwards."""
        if ms is None or ms <= self._ms:
            self._counter += 1
            if self._counter > _MAX_COUNTER:
                # 4.4 trillion rows in one millisecond is not a real database,
                # but the counter must not carry into the variant bits.
                self._ms += 1
                self._counter = 0
        else:
            self._ms = ms
            self._counter = 0
        return uuid7_at(self._ms, self._counter)
