"""
The five numbers that define "healthy" (Sprint 9).

| Signal              | Definition                                                        |
|---------------------|-------------------------------------------------------------------|
| Call success rate   | terminated calls ending COMPLETED / all terminated                |
| Booking success     | appointment calls confirmed / attempted (cooperative outcomes)    |
| Turn latency        | p50/p95 voice-to-voice, from the Sprint 5 turn records            |
| Stuck calls         | calls still active past max_duration + grace                      |
| Cost per call       | p50/p95 total per call_sid vs. the 7-day baseline                 |

Computed from SQLite and the turn-latency JSONL rather than queried back out of
the metrics backend, deliberately: alerting, the CLI, and the dashboard must
work on a bare install with no OTel collector, and an alert rule that depends on
the monitoring stack being up cannot tell you the monitoring stack is down.

Every signal carries `sample_size` and `sufficient_data`. A rule that fires on
one failed call out of one is worse than no rule — it teaches an operator to
ignore the pager.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import aiosqlite

from pincer.observability.failure_codes import EXCLUDED_FROM_SLO, FailureCode

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pincer.config import Settings

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    """One golden signal: a value, the sample it came from, and its target."""

    name: str
    value: float | None
    unit: str = ""
    sample_size: int = 0
    min_sample: int = 1
    target: float | None = None
    window: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def sufficient_data(self) -> bool:
        return self.value is not None and self.sample_size >= self.min_sample

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "sufficient_data": self.sufficient_data}


@dataclass
class GoldenSignals:
    call_success_rate: Signal
    booking_success_rate: Signal
    turn_latency: Signal
    stuck_calls: Signal
    cost_per_call: Signal
    generated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    # Sprint 12 §10.3: inbound calls declined for capacity in the last 24h (alarm > 5/day)
    busy_capacity: Signal | None = None
    # Negative-sentiment calls in the last 24h (alarm at 3/day)
    negative_sentiment: Signal | None = None

    def all(self) -> list[Signal]:
        signals = [
            self.call_success_rate,
            self.booking_success_rate,
            self.turn_latency,
            self.stuck_calls,
            self.cost_per_call,
        ]
        if self.busy_capacity is not None:
            signals.append(self.busy_capacity)
        if self.negative_sentiment is not None:
            signals.append(self.negative_sentiment)
        return signals

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "signals": {s.name: s.to_dict() for s in self.all()},
        }


@asynccontextmanager
async def _db(settings: Settings | Any) -> AsyncIterator[aiosqlite.Connection]:
    async with aiosqlite.connect(str(settings.db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        yield conn


def _cutoff(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


def percentile(values: list[float], pct: float) -> float | None:
    """Linear-interpolated percentile; None for an empty sample."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * pct
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


async def _table_exists(conn: aiosqlite.Connection, table: str) -> bool:
    cursor = await conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return await cursor.fetchone() is not None


# ── 1. Call success rate ─────────────────────────────────────────────


async def call_success_rate(settings: Settings | Any, window_hours: float | None = None) -> Signal:
    """Terminated calls that ended COMPLETED, over all terminated calls."""
    hours = float(window_hours if window_hours is not None else getattr(settings, "alert_call_success_window_h", 2))
    min_sample = int(getattr(settings, "alert_call_success_min_volume", 5))
    target = float(getattr(settings, "alert_call_success_min", 0.85))

    by_code: dict[str, int] = {}
    total = 0
    completed = 0
    try:
        async with _db(settings) as conn:
            if not await _table_exists(conn, "voice_calls"):
                return Signal("call_success_rate", None, "ratio", 0, min_sample, target, f"{hours:g}h")
            rows = await conn.execute_fetchall(
                "SELECT failure_code FROM voice_calls WHERE ended_at IS NOT NULL AND started_at >= ?",
                (_cutoff(hours),),
            )
    except aiosqlite.OperationalError:
        # failure_code predates nothing else — a DB from before the Sprint 9
        # migration simply has no data for this signal yet.
        return Signal("call_success_rate", None, "ratio", 0, min_sample, target, f"{hours:g}h")

    for row in rows:
        total += 1
        code = str(row["failure_code"] or FailureCode.NONE)
        by_code[code] = by_code.get(code, 0) + 1
        if code == FailureCode.NONE:
            completed += 1

    return Signal(
        name="call_success_rate",
        value=(completed / total) if total else None,
        unit="ratio",
        sample_size=total,
        min_sample=min_sample,
        target=target,
        window=f"{hours:g}h",
        detail={"completed": completed, "terminated": total, "by_failure_code": by_code},
    )


async def call_attempt_success_rate(settings: Settings | Any, window_hours: float = 720.0) -> Signal:
    """The SLI behind the T9.5 SLO: successes over attempts we could influence.

    Calls that failed because the callee was unreachable or because a policy
    deliberately blocked them are removed from the denominator — an SLO that
    burns budget for a callee being on holiday cannot drive any decision.
    """
    target = float(getattr(settings, "slo_call_attempt_success", 0.99))
    excluded = {str(c) for c in EXCLUDED_FROM_SLO}

    eligible = 0
    completed = 0
    excluded_count = 0
    try:
        async with _db(settings) as conn:
            if not await _table_exists(conn, "voice_calls"):
                return Signal("call_attempt_success_rate", None, "ratio", 0, 1, target, f"{window_hours:g}h")
            rows = await conn.execute_fetchall(
                "SELECT failure_code FROM voice_calls WHERE ended_at IS NOT NULL AND started_at >= ?",
                (_cutoff(window_hours),),
            )
    except aiosqlite.OperationalError:
        return Signal("call_attempt_success_rate", None, "ratio", 0, 1, target, f"{window_hours:g}h")

    for row in rows:
        code = str(row["failure_code"] or FailureCode.NONE)
        if code in excluded:
            excluded_count += 1
            continue
        eligible += 1
        if code == FailureCode.NONE:
            completed += 1

    return Signal(
        name="call_attempt_success_rate",
        value=(completed / eligible) if eligible else None,
        unit="ratio",
        sample_size=eligible,
        target=target,
        window=f"{window_hours:g}h",
        detail={"completed": completed, "eligible": eligible, "excluded_callee_or_policy": excluded_count},
    )


# ── 2. Booking success rate ──────────────────────────────────────────


async def booking_success_rate(settings: Settings | Any, window_hours: float | None = None) -> Signal:
    """Appointment calls that produced a confirmed slot, over cooperative attempts.

    "Cooperative" = the callee actually engaged. A voicemail or a no-answer says
    nothing about whether the agent can negotiate a time, so counting them would
    turn this into a duplicate of the call success rate.
    """
    hours = float(window_hours if window_hours is not None else getattr(settings, "alert_booking_window_h", 24))
    min_sample = int(getattr(settings, "alert_booking_min_volume", 3))
    target = float(getattr(settings, "alert_booking_success_min", 0.70))

    try:
        async with _db(settings) as conn:
            if not await _table_exists(conn, "appointment_outcomes"):
                return Signal("booking_success_rate", None, "ratio", 0, min_sample, target, f"{hours:g}h")
            rows = await conn.execute_fetchall(
                "SELECT result FROM appointment_outcomes WHERE recorded_at >= ?",
                (_cutoff(hours),),
            )
    except aiosqlite.OperationalError:
        return Signal("booking_success_rate", None, "ratio", 0, min_sample, target, f"{hours:g}h")

    by_result: dict[str, int] = {}
    for row in rows:
        result = str(row["result"] or "unknown")
        by_result[result] = by_result.get(result, 0) + 1

    cooperative = sum(count for result, count in by_result.items() if result not in ("unreachable", "voicemail"))
    confirmed = by_result.get("confirmed", 0)

    return Signal(
        name="booking_success_rate",
        value=(confirmed / cooperative) if cooperative else None,
        unit="ratio",
        sample_size=cooperative,
        min_sample=min_sample,
        target=target,
        window=f"{hours:g}h",
        detail={"confirmed": confirmed, "cooperative_attempts": cooperative, "by_result": by_result},
    )


# ── 3. Turn latency ──────────────────────────────────────────────────


def _read_turn_latencies(settings: Settings | Any, window_hours: float) -> list[float]:
    """Total-ms values from voice_latency.jsonl inside the window, as seconds."""
    from pincer.voice.latency_report import read_turn_records

    data_dir = getattr(settings, "data_dir", None)
    if data_dir is None:
        return []
    path = data_dir / "logs" / "voice_latency.jsonl"
    cutoff = datetime.now(UTC) - timedelta(hours=window_hours)

    values: list[float] = []
    # A big window still only needs recent calls; 500 is far more than any
    # pilot produces in a day and bounds the parse cost.
    for record in read_turn_records(path, last_calls=500):
        total_ms = record.get("total_ms")
        if not isinstance(total_ms, int | float):
            continue
        raw_ts = str(record.get("ts", ""))
        if raw_ts:
            try:
                stamp = datetime.fromisoformat(raw_ts)
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=UTC)
                if stamp < cutoff:
                    continue
            except ValueError:
                pass
        values.append(float(total_ms) / 1000.0)
    return values


async def turn_latency(settings: Settings | Any, window_hours: float | None = None) -> Signal:
    """p95 voice-to-voice turn latency; `value` is the p95 (what alerts on)."""
    hours = float(window_hours if window_hours is not None else getattr(settings, "alert_latency_window_h", 1))
    min_sample = int(getattr(settings, "alert_latency_min_turns", 10))
    target = float(getattr(settings, "alert_latency_p95_max_s", 2.5))

    values = _read_turn_latencies(settings, hours)
    p50 = percentile(values, 0.50)
    p95 = percentile(values, 0.95)

    return Signal(
        name="turn_latency_p95",
        value=p95,
        unit="s",
        sample_size=len(values),
        min_sample=min_sample,
        target=target,
        window=f"{hours:g}h",
        detail={
            "p50_s": round(p50, 3) if p50 is not None else None,
            "p95_s": round(p95, 3) if p95 is not None else None,
            "slo_p95_s": float(getattr(settings, "slo_latency_p95_s", 2.0)),
            "turns": len(values),
        },
    )


# ── 4. Stuck calls ───────────────────────────────────────────────────


def stuck_calls(settings: Settings | Any, active_calls: dict[str, Any] | None = None) -> Signal:
    """Calls still running past `voice_max_call_duration` + grace.

    Reads live engine state rather than the database: a stuck call is by
    definition one that never wrote an `ended_at`, and it pages immediately —
    it burns money and holds a line open every second it survives.
    """
    max_duration = int(getattr(settings, "voice_max_call_duration", 600) or 600)
    grace = int(getattr(settings, "alert_stuck_call_grace_s", 60) or 0)
    threshold = max_duration + grace

    if active_calls is None:
        try:
            from pincer.voice.twiml_server import get_engine

            engine = get_engine()
            active_calls = engine.get_active_calls() if engine else {}
        except Exception:
            active_calls = {}

    stuck: list[dict[str, Any]] = []
    for call_sid, state in (active_calls or {}).items():
        duration = int(getattr(state, "duration_seconds", 0) or 0)
        if duration > threshold:
            stuck.append(
                {
                    "call_sid": call_sid,
                    "duration_seconds": duration,
                    "direction": str(getattr(state, "direction", "")),
                    "over_by_seconds": duration - threshold,
                }
            )

    return Signal(
        name="stuck_calls",
        value=float(len(stuck)),
        unit="count",
        sample_size=len(active_calls or {}),
        min_sample=0,
        target=0.0,
        window="live",
        detail={"threshold_seconds": threshold, "active_calls": len(active_calls or {}), "stuck": stuck},
    )


# ── 5. Cost per call ─────────────────────────────────────────────────


async def cost_per_call(settings: Settings | Any, window_hours: float = 24.0) -> Signal:
    """p95 cost per call in the window, against the rolling baseline p95.

    `value` is the *ratio* to the baseline rather than an absolute: an absolute
    threshold would need retuning every time prices or call mix change, whereas
    "twice what it normally costs" stays meaningful.
    """
    baseline_days = int(getattr(settings, "alert_cost_baseline_days", 7))
    multiplier = float(getattr(settings, "alert_cost_p95_multiplier", 2.0))
    min_calls = int(getattr(settings, "alert_cost_min_calls", 10))

    async def _totals(start_hours_ago: float, end_hours_ago: float = 0.0) -> list[float]:
        async with _db(settings) as conn:
            if not await _table_exists(conn, "call_costs"):
                return []
            rows = await conn.execute_fetchall(
                "SELECT total_usd FROM call_costs WHERE recorded_at >= ? AND recorded_at < ?",
                (_cutoff(start_hours_ago), _cutoff(end_hours_ago)),
            )
        return [float(r["total_usd"] or 0.0) for r in rows]

    try:
        recent = await _totals(window_hours)
        # The baseline must EXCLUDE the window being judged. Overlapping them
        # lets a spike raise its own baseline: today's expensive calls land in
        # both p95s, the ratio collapses toward 1.0, and the alert never fires
        # — the exact failure this rule exists to catch.
        baseline = await _totals(baseline_days * 24.0, window_hours)
    except aiosqlite.OperationalError:
        recent, baseline = [], []

    recent_p95 = percentile(recent, 0.95)
    baseline_p95 = percentile(baseline, 0.95)
    ratio = (recent_p95 / baseline_p95) if (recent_p95 and baseline_p95) else None

    return Signal(
        name="cost_per_call",
        value=ratio,
        unit="ratio_to_baseline",
        sample_size=len(baseline),
        min_sample=min_calls,
        target=multiplier,
        window=f"{window_hours:g}h vs prior {baseline_days}d",
        detail={
            "recent_p50_usd": round(percentile(recent, 0.50) or 0.0, 4),
            "recent_p95_usd": round(recent_p95, 4) if recent_p95 else None,
            "baseline_p95_usd": round(baseline_p95, 4) if baseline_p95 else None,
            "calls_in_window": len(recent),
            "calls_in_baseline": len(baseline),
        },
    )


# ── All five at once ─────────────────────────────────────────────────


BUSY_CAPACITY_DAILY_THRESHOLD = 5


async def busy_capacity(settings: Settings | Any, window_hours: float = 24.0) -> Signal:
    """Sprint 12 §10.3: inbound calls answered with the busy line (failure_code
    busy_capacity) in the window. Alarm when the count exceeds 5/day."""
    count = 0
    try:
        async with _db(settings) as conn:
            if await _table_exists(conn, "voice_calls"):
                row = await (
                    await conn.execute(
                        "SELECT COUNT(*) AS n FROM voice_calls WHERE failure_code = ? AND started_at >= ?",
                        (FailureCode.BUSY_CAPACITY.value, _cutoff(window_hours)),
                    )
                ).fetchone()
                count = int(row["n"] if row else 0)
    except Exception:
        logger.debug("busy_capacity signal failed", exc_info=True)
    return Signal(
        name="busy_capacity",
        value=float(count),
        unit="count",
        sample_size=count,
        min_sample=0,
        target=float(BUSY_CAPACITY_DAILY_THRESHOLD),
        window=f"{int(window_hours)}h",
        detail={"declined": count, "threshold_per_day": BUSY_CAPACITY_DAILY_THRESHOLD},
    )


NEGATIVE_SENTIMENT_DAILY_THRESHOLD = 3


async def negative_sentiment(settings: Settings | Any, window_hours: float = 24.0) -> Signal:
    """Calls the caller seemed unhappy about, in the window.

    Three in a day is not a statistical claim — it is "go listen to these".
    The signal deliberately counts calls, not a rate: on a low-volume line a
    rate would swing wildly and either scream or stay silent for the wrong
    reasons.
    """
    from pincer.voice.analytics import count_negative_since

    count = 0
    try:
        count = await count_negative_since(settings.db_path, _cutoff(window_hours))
    except Exception:
        logger.debug("negative_sentiment signal failed", exc_info=True)
    return Signal(
        name="negative_sentiment",
        value=float(count),
        unit="count",
        sample_size=count,
        min_sample=0,
        target=float(NEGATIVE_SENTIMENT_DAILY_THRESHOLD),
        window=f"{int(window_hours)}h",
        detail={"negative_calls": count, "threshold_per_day": NEGATIVE_SENTIMENT_DAILY_THRESHOLD},
    )


async def collect(settings: Settings | Any, active_calls: dict[str, Any] | None = None) -> GoldenSignals:
    """Every golden signal, each in its own configured window."""
    return GoldenSignals(
        call_success_rate=await call_success_rate(settings),
        booking_success_rate=await booking_success_rate(settings),
        turn_latency=await turn_latency(settings),
        stuck_calls=stuck_calls(settings, active_calls),
        cost_per_call=await cost_per_call(settings),
        busy_capacity=await busy_capacity(settings),
        negative_sentiment=await negative_sentiment(settings),
    )
