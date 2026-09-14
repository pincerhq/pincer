"""
Clock discipline for telephony telemetry.

Two clocks, two jobs, never mixed:

* ``time.monotonic_ns()`` — the only clock durations are computed from. It
  cannot jump backwards on an NTP correction, so a 400 ms stage stays 400 ms.
  It is meaningless outside this process.
* ``datetime.now(UTC)`` — the only clock events are *ordered* by across
  processes (our API, Twilio's webhooks, a future worker). It can jump.

The rule this module enforces in code rather than in review comments:
**a duration is only ever produced from two monotonic readings taken in the
same process.** Subtracting a Twilio webhook timestamp from one of ours would
silently encode the clock skew between two machines as latency, so
`cross_process_gap` refuses to return a number and returns an explanation
instead.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime

# One process-wide anchor pairing the two clocks. Taken once at import so every
# monotonic stamp in this process can be projected onto a wall clock with the
# same offset — projections stay mutually consistent even if the wall clock is
# stepped mid-call.
_ANCHOR_MONO_NS = time.monotonic_ns()
_ANCHOR_UTC = datetime.now(UTC)


def mono_ns() -> int:
    """Monotonic nanoseconds. The only input to a duration."""
    return time.monotonic_ns()


def now_utc() -> datetime:
    """Wall-clock UTC. The only input to cross-process ordering."""
    return datetime.now(UTC)


def utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def project_utc(mono_value_ns: int) -> datetime:
    """Wall-clock time a monotonic stamp from *this process* corresponds to.

    Used so a span recorded with monotonic precision can still be placed on a
    timeline next to provider webhooks. The projection inherits the anchor's
    accuracy — it is good enough to order events on a UI timeline, and it is
    never used to compute a duration.
    """
    return _ANCHOR_UTC + _timedelta_ns(mono_value_ns - _ANCHOR_MONO_NS)


def _timedelta_ns(delta_ns: int):  # type: ignore[no-untyped-def]
    from datetime import timedelta

    return timedelta(microseconds=delta_ns / 1000.0)


def duration_ms(start_ns: int | None, end_ns: int | None) -> float | None:
    """Milliseconds between two monotonic readings from this process.

    ``None`` when either end is missing — a missing boundary is reported as
    "not measured", never as zero.
    """
    if start_ns is None or end_ns is None:
        return None
    return (end_ns - start_ns) / 1_000_000.0


def cross_process_gap(_ours_utc: datetime, _theirs_utc: datetime) -> None:
    """Deliberately returns nothing.

    Kept as a named function so the intent is greppable: the gap between a
    provider's timestamp and ours is clock skew plus latency and we cannot
    separate them. Call sites that want this number must instead record both
    timestamps and let the UI show them side by side, labelled as such.
    """
    return None


@dataclass(slots=True)
class Stopwatch:
    """A monotonic origin paired with the wall clock it was started at.

    One per call and one per turn. Every stage stamp is an offset from this
    origin, which is what makes the waterfall renderable without trusting the
    wall clock, and what makes ``total`` equal to the sum of a partitioned
    critical path exactly rather than approximately.
    """

    started_ns: int
    started_utc: datetime

    @classmethod
    def start(cls) -> Stopwatch:
        return cls(started_ns=mono_ns(), started_utc=now_utc())

    def elapsed_ms(self) -> float:
        return (mono_ns() - self.started_ns) / 1_000_000.0

    def offset_ms(self, at_ns: int) -> float:
        return (at_ns - self.started_ns) / 1_000_000.0

    def utc_at(self, at_ns: int) -> datetime:
        return self.started_utc + _timedelta_ns(at_ns - self.started_ns)
