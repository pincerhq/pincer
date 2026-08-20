"""
Weekly failure digest (Sprint 9, T9.3).

Alerts tell you something is wrong *now*. The digest answers the question alerts
structurally cannot: **what has been quietly going wrong all week, and is it
getting worse?**

That "getting worse" part is why every number is rendered with a delta against
the previous week. A digest that lists "no_answer ×14" every Monday teaches
nothing; "no_answer ×14 (+9)" is a signal. Codes are ranked by absolute count,
but a code that *appeared this week and did not exist last week* is called out
separately — new failure modes are the ones worth a human's Monday morning.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import aiosqlite

from pincer.observability.failure_codes import FailureCode, describe

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pincer.config import Settings

logger = logging.getLogger(__name__)

WEEK_HOURS = 168.0


@asynccontextmanager
async def _db(settings: Settings | Any) -> AsyncIterator[aiosqlite.Connection]:
    async with aiosqlite.connect(str(settings.db_path)) as conn:
        conn.row_factory = aiosqlite.Row
        yield conn


def _iso(hours_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()


@dataclass
class DigestPeriod:
    calls: int = 0
    completed: int = 0
    by_code: dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0

    @property
    def success_rate(self) -> float | None:
        return (self.completed / self.calls) if self.calls else None


async def _period(settings: Settings | Any, start_hours_ago: float, end_hours_ago: float) -> DigestPeriod:
    """Aggregate one window: `start_hours_ago` back to `end_hours_ago` ago."""
    period = DigestPeriod()
    try:
        async with _db(settings) as conn:
            rows = await conn.execute_fetchall(
                "SELECT failure_code FROM voice_calls WHERE ended_at IS NOT NULL "
                "AND started_at >= ? AND started_at < ?",
                (_iso(start_hours_ago), _iso(end_hours_ago)),
            )
            for row in rows:
                code = str(row["failure_code"] or FailureCode.NONE)
                period.calls += 1
                if code == FailureCode.NONE:
                    period.completed += 1
                else:
                    period.by_code[code] = period.by_code.get(code, 0) + 1

            cursor = await conn.execute(
                "SELECT COALESCE(SUM(total_usd), 0) AS spend FROM call_costs "
                "WHERE recorded_at >= ? AND recorded_at < ?",
                (_iso(start_hours_ago), _iso(end_hours_ago)),
            )
            cost_row = await cursor.fetchone()
            if cost_row is not None:
                period.cost_usd = float(cost_row["spend"] or 0.0)
    except aiosqlite.OperationalError:
        # Tables not created yet — an empty period is the honest answer.
        pass
    except Exception:
        logger.exception("Digest period aggregation failed")
    return period


def _fmt_delta(current: int, previous: int) -> str:
    delta = current - previous
    if previous == 0 and current > 0:
        return " (NEW)"
    if delta == 0:
        return " (=)"
    return f" ({delta:+d})"


async def build_digest(settings: Settings | Any) -> str:
    """Render the weekly digest. Always returns text, even with no data."""
    this_week = await _period(settings, WEEK_HOURS, 0)
    last_week = await _period(settings, WEEK_HOURS * 2, WEEK_HOURS)

    lines = ["📊 *Voice weekly digest*", ""]

    if this_week.calls == 0:
        lines.append("No calls completed in the last 7 days.")
        if last_week.calls:
            lines.append(f"(Previous week: {last_week.calls} calls — traffic stopped, worth checking why.)")
        return "\n".join(lines)

    rate = this_week.success_rate or 0.0
    prev_rate = last_week.success_rate
    rate_delta = f" ({(rate - prev_rate) * 100:+.0f}pp)" if prev_rate is not None else ""
    lines.append(
        f"*Calls:* {this_week.calls} ({this_week.calls - last_week.calls:+d} vs last week) · "
        f"*success* {rate:.0%}{rate_delta}"
    )

    if this_week.cost_usd or last_week.cost_usd:
        per_call = this_week.cost_usd / this_week.calls if this_week.calls else 0.0
        cost_delta = f" ({this_week.cost_usd - last_week.cost_usd:+.2f})" if last_week.cost_usd else ""
        lines.append(f"*Spend:* ${this_week.cost_usd:.2f}{cost_delta} · ${per_call:.3f}/call")

    # ── Failure codes, ranked, with deltas ──
    if this_week.by_code:
        lines.extend(["", "*Top failure codes:*"])
        ranked = sorted(this_week.by_code.items(), key=lambda kv: kv[1], reverse=True)
        for code, count in ranked[:6]:
            delta = _fmt_delta(count, last_week.by_code.get(code, 0))
            lines.append(f"  `{code}` ×{count}{delta} — {describe(code)}")

        new_codes = [c for c in this_week.by_code if c not in last_week.by_code]
        if new_codes:
            lines.extend(["", f"🆕 *New failure modes this week:* {', '.join(f'`{c}`' for c in new_codes)}"])
    else:
        lines.extend(["", "No failures recorded this week. 🎉"])

    # Outside the branch above on purpose: a week with ZERO failures is exactly
    # when "cleared since last week" matters most, and nesting it under
    # `if this_week.by_code` made it unreachable in that case.
    gone = [c for c, n in last_week.by_code.items() if n >= 3 and c not in this_week.by_code]
    if gone:
        lines.append(f"✅ *Cleared since last week:* {', '.join(f'`{c}`' for c in gone)}")

    # ── Bookings ──
    try:
        from pincer.observability.bookings import booking_breakdown

        bookings = await booking_breakdown(settings, WEEK_HOURS)
        if bookings:
            confirmed = bookings.get("confirmed", 0)
            total = sum(bookings.values())
            lines.extend(["", f"*Bookings:* {confirmed}/{total} confirmed · {bookings}"])
    except Exception:
        logger.debug("Digest booking breakdown failed", exc_info=True)

    # ── SLO / error budget ──
    try:
        from pincer.observability.slo import collect as collect_slos

        slo_report = await collect_slos(settings)
        rendered = [f"  {s['name']}: {_fmt_slo(s)}" for s in slo_report["slos"] if s.get("actual") is not None]
        if rendered:
            lines.extend(["", "*SLOs (month to date):*", *rendered])
        if slo_report.get("feature_freeze"):
            lines.extend(["", f"🧊 *Feature freeze in effect:* {slo_report['freeze_reason']}"])
    except Exception:
        logger.debug("Digest SLO section failed", exc_info=True)

    # ── Canary coverage ──
    try:
        from pincer.observability.canary import recent_runs

        runs = await recent_runs(settings, limit=50)
        week_runs = [r for r in runs if str(r.get("ran_at", "")) >= _iso(WEEK_HOURS)]
        if week_runs:
            failed = sum(1 for r in week_runs if not r["ok"])
            skipped = sum(1 for r in week_runs if r["skipped"])
            lines.extend(["", f"*Canary:* {len(week_runs)} run(s), {failed} failed, {skipped} skipped"])
    except Exception:
        logger.debug("Digest canary section failed", exc_info=True)

    lines.extend(["", "Runbook: docs/operations/runbook.md"])
    return "\n".join(lines)


def _fmt_slo(status: dict[str, Any]) -> str:
    unit = status.get("unit", "")
    actual = status.get("actual")
    target = status.get("target")
    value = f"{actual:.1%}" if unit == "ratio" else f"{actual:.2f}{unit}"
    goal = f"{target:.1%}" if unit == "ratio" else f"{target:.2f}{unit}"
    mark = "✅" if status.get("met") else "❌"
    burn = status.get("burn_pct")
    burn_text = f", {burn:.0f}% budget burned" if burn is not None else ""
    return f"{mark} {value} (target {goal}{burn_text})"


def make_digest_handler(settings: Settings | Any) -> Any:
    """Build the CronScheduler action handler for ``voice_weekly_digest``."""

    async def _handler(pincer_user_id: str, action: dict[str, Any], channel: str) -> str | None:
        return await build_digest(settings)

    return _handler
