"""
SLO tracking and the error budget (Sprint 9, T9.5).

The pilot SLOs are *measured*, not aspirational — each one is an SLI this
codebase already computes:

| SLO                    | Target | SLI source                                    |
|------------------------|--------|-----------------------------------------------|
| Call attempt success   | 99%    | `voice_calls.failure_code`, callee/policy      |
|                        |        | outcomes excluded from the denominator         |
| Turn latency p95       | ≤ 2.0s | `voice_latency.jsonl` turn records             |
| Report delivery        | ≤ 30s  | hangup → post-call report timestamp            |
| Monthly availability   | 99.5%  | canary runs + service-down alert minutes       |

**Error budget.** For a 99% target the budget is the other 1%: over a month of
N eligible attempts, N × 0.01 may fail before the SLO is missed. `burn_pct` is
how much of that has been spent so far this month. The agreed rule (T9.5) is
that burning more than `slo_error_budget_freeze_pct` mid-month stops feature
work until it recovers — which is only enforceable if the number is on a
dashboard, so `error_budget()` is what the API and the CLI both render.

Availability is deliberately the weakest of the four: without an external prober
we can only infer it from canary results and the alert record, so it is reported
with its own `confidence` field rather than presented as measured fact.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pincer.config import Settings

logger = logging.getLogger(__name__)

# Minimum observations before an SLO's error budget can trigger a feature freeze.
MIN_BUDGET_SAMPLE = 100


@dataclass
class SLOStatus:
    """One SLO: where it stands, and how much budget it has burned."""

    name: str
    target: float
    actual: float | None
    unit: str
    window: str
    sample_size: int = 0
    budget_total: float | None = None
    budget_spent: float | None = None
    burn_pct: float | None = None
    met: bool | None = None
    confidence: str = "measured"  # measured | inferred
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _month_window_hours(now: datetime | None = None) -> tuple[float, str]:
    """Hours elapsed in the current calendar month, and a label."""
    moment = now or datetime.now(UTC)
    month_start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    hours = max(1.0, (moment - month_start).total_seconds() / 3600.0)
    return hours, moment.strftime("%Y-%m")


async def call_success_slo(settings: Settings | Any, now: datetime | None = None) -> SLOStatus:
    """99% of call attempts succeed, excluding callee no-answer and policy blocks."""
    from pincer.observability.golden_signals import call_attempt_success_rate

    hours, label = _month_window_hours(now)
    target = float(getattr(settings, "slo_call_attempt_success", 0.99))
    signal = await call_attempt_success_rate(settings, window_hours=hours)

    attempts = signal.sample_size
    failures = attempts - int(signal.detail.get("completed", 0))
    budget_total = attempts * (1.0 - target)
    burn_pct = (failures / budget_total * 100.0) if budget_total > 0 else None

    return SLOStatus(
        name="call_attempt_success",
        target=target,
        actual=signal.value,
        unit="ratio",
        window=f"month {label}",
        sample_size=attempts,
        budget_total=round(budget_total, 2) if budget_total else 0.0,
        budget_spent=float(failures),
        burn_pct=round(burn_pct, 1) if burn_pct is not None else None,
        met=(signal.value >= target) if signal.value is not None else None,
        detail=signal.detail,
    )


async def latency_slo(settings: Settings | Any, now: datetime | None = None) -> SLOStatus:
    """p95 voice-to-voice turn latency ≤ 2.0s.

    Budget for a latency SLO is expressed as "turns slower than the target",
    the standard formulation: a p95 target means at most 5% of turns may exceed
    it, so the budget is 5% of the month's turns.
    """
    from pincer.observability.golden_signals import _read_turn_latencies

    hours, label = _month_window_hours(now)
    target = float(getattr(settings, "slo_latency_p95_s", 2.0))
    values = _read_turn_latencies(settings, hours)

    if not values:
        return SLOStatus(
            name="turn_latency_p95",
            target=target,
            actual=None,
            unit="s",
            window=f"month {label}",
            sample_size=0,
        )

    from pincer.observability.golden_signals import percentile

    p95 = percentile(values, 0.95)
    slow = sum(1 for v in values if v > target)
    budget_total = len(values) * 0.05
    burn_pct = (slow / budget_total * 100.0) if budget_total > 0 else None

    return SLOStatus(
        name="turn_latency_p95",
        target=target,
        actual=p95,
        unit="s",
        window=f"month {label}",
        sample_size=len(values),
        budget_total=round(budget_total, 1),
        budget_spent=float(slow),
        burn_pct=round(burn_pct, 1) if burn_pct is not None else None,
        met=(p95 is not None and p95 <= target),
        detail={
            "p50_s": round(percentile(values, 0.50) or 0.0, 3),
            "p95_s": round(p95, 3) if p95 else None,
            "turns_over_target": slow,
        },
    )


async def report_delivery_slo(settings: Settings | Any, now: datetime | None = None) -> SLOStatus:
    """Post-call report reaches the initiating user within 30s of hangup."""
    import aiosqlite

    hours, label = _month_window_hours(now)
    target = float(getattr(settings, "slo_report_delivery_s", 30.0))

    from pincer.observability.golden_signals import _cutoff, percentile

    delays: list[float] = []
    try:
        async with aiosqlite.connect(str(settings.db_path)) as conn:
            conn.row_factory = aiosqlite.Row
            rows = await conn.execute_fetchall(
                "SELECT ended_at, report_delivered_at FROM voice_calls "
                "WHERE ended_at IS NOT NULL AND report_delivered_at IS NOT NULL AND started_at >= ?",
                (_cutoff(hours),),
            )
    except Exception:
        rows = []

    for row in rows:
        try:
            ended = datetime.fromisoformat(str(row["ended_at"]))
            delivered = datetime.fromisoformat(str(row["report_delivered_at"]))
        except (ValueError, TypeError):
            continue
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=UTC)
        if delivered.tzinfo is None:
            delivered = delivered.replace(tzinfo=UTC)
        delays.append(max(0.0, (delivered - ended).total_seconds()))

    if not delays:
        return SLOStatus(
            name="report_delivery",
            target=target,
            actual=None,
            unit="s",
            window=f"month {label}",
            sample_size=0,
        )

    p95 = percentile(delays, 0.95)
    late = sum(1 for d in delays if d > target)
    budget_total = len(delays) * 0.05
    return SLOStatus(
        name="report_delivery",
        target=target,
        actual=p95,
        unit="s",
        window=f"month {label}",
        sample_size=len(delays),
        budget_total=round(budget_total, 1),
        budget_spent=float(late),
        burn_pct=round(late / budget_total * 100.0, 1) if budget_total > 0 else None,
        met=(p95 is not None and p95 <= target),
        detail={"p50_s": round(percentile(delays, 0.50) or 0.0, 2), "reports_over_target": late},
    )


async def availability_slo(settings: Settings | Any, now: datetime | None = None) -> SLOStatus:
    """Monthly availability, inferred from canary results.

    Marked `inferred`, not `measured`: without an external prober we only know
    the service was up at the moments the canary ran. Reported honestly rather
    than presented as a measured number — an SLO nobody can audit is worse than
    an SLO that admits its own uncertainty.
    """
    import aiosqlite

    hours, label = _month_window_hours(now)
    target = float(getattr(settings, "slo_availability", 0.995))

    from pincer.observability.golden_signals import _cutoff

    ok = 0
    total = 0
    try:
        async with aiosqlite.connect(str(settings.db_path)) as conn:
            conn.row_factory = aiosqlite.Row
            rows = await conn.execute_fetchall(
                "SELECT ok FROM canary_runs WHERE ran_at >= ?",
                (_cutoff(hours),),
            )
        for row in rows:
            total += 1
            ok += 1 if row["ok"] else 0
    except Exception:
        total, ok = 0, 0

    actual = (ok / total) if total else None
    budget_total = total * (1.0 - target)
    failures = total - ok
    return SLOStatus(
        name="availability",
        target=target,
        actual=actual,
        unit="ratio",
        window=f"month {label}",
        sample_size=total,
        budget_total=round(budget_total, 2) if budget_total else 0.0,
        budget_spent=float(failures),
        burn_pct=round(failures / budget_total * 100.0, 1) if budget_total > 0 else None,
        met=(actual >= target) if actual is not None else None,
        confidence="inferred",
        detail={"canary_runs": total, "canary_ok": ok},
    )


async def collect(settings: Settings | Any, now: datetime | None = None) -> dict[str, Any]:
    """Every SLO plus the freeze verdict — what the dashboard and CLI render."""
    statuses = [
        await call_success_slo(settings, now),
        await latency_slo(settings, now),
        await report_delivery_slo(settings, now),
        await availability_slo(settings, now),
    ]
    freeze_pct = float(getattr(settings, "slo_error_budget_freeze_pct", 50.0))
    # A freeze is an expensive organisational decision, so it needs a sample
    # worth deciding on. Without this guard a handful of slow dev turns declares
    # a company-wide feature freeze, and the rule gets ignored the first time.
    burning = [
        s for s in statuses if s.burn_pct is not None and s.burn_pct > freeze_pct and s.sample_size >= MIN_BUDGET_SAMPLE
    ]

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "freeze_threshold_pct": freeze_pct,
        "freeze_min_sample": MIN_BUDGET_SAMPLE,
        "feature_freeze": bool(burning),
        "freeze_reason": (
            "; ".join(f"{s.name} burned {s.burn_pct:.0f}% of its error budget" for s in burning) if burning else ""
        ),
        "slos": [s.to_dict() for s in statuses],
    }
