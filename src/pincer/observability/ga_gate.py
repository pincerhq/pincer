"""
GA gate evaluation (Sprint 10, T10.4) — the exit criteria, measured.

The sign-off meeting is only worth holding if the checklist is evidence-based,
so every criterion here is evaluated against real data from the pilot period and
reports the numbers it used. Nobody has to take anyone's word for it, and nobody
has to hand-assemble a spreadsheet the night before.

The verdicts are deliberately four, not two:

``PASS``            met, with the evidence attached
``FAIL``            measured and missed — the number is right there
``INSUFFICIENT``    not enough data to judge. **This is not a pass.** A gate
                    that reads "100% success" off three calls is how a product
                    ships on a lie; the criterion says how much more it needs.
``MANUAL``          genuinely requires a human (testimonials, "were the alerts
                    actionable?"). The tool states what to bring; it never
                    invents an answer.

Criteria come from the roadmap, so the thresholds live in `GAThresholds` rather
than in `Settings`: they are the definition of GA, not deployment tuning, and
changing one should show up in a diff of this file.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import aiosqlite

from pincer.observability.failure_codes import EXCLUDED_FROM_SLO, FailureCode

if TYPE_CHECKING:
    from pincer.config import Settings

logger = logging.getLogger(__name__)


class Verdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INSUFFICIENT = "insufficient_data"
    MANUAL = "manual"


@dataclass
class GAThresholds:
    """The roadmap exit criteria. Changing these changes what GA means."""

    min_calls: int = 200
    call_success_min: float = 0.95
    max_stuck_calls: int = 0
    booking_cooperative_min: float = 0.80
    booking_overall_min: float = 0.70
    min_bookings: int = 20
    latency_p50_max_s: float = 1.2
    latency_p95_max_s: float = 2.0
    min_turns: int = 200
    cost_per_call_min_usd: float = 0.12
    cost_per_call_max_usd: float = 0.25
    min_priced_calls: int = 50
    # Languages that must each independently meet the latency bar. A product
    # sold into DACH cannot pass on an English-only sample.
    required_languages: tuple[str, ...] = ("en", "de")
    min_turns_per_language: int = 50


@dataclass
class Criterion:
    key: str
    title: str
    verdict: Verdict
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)
    needed: str = ""  # what would move INSUFFICIENT/MANUAL to a decision

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GAGateReport:
    generated_at: str
    window_days: int
    thresholds: dict[str, Any]
    criteria: list[Criterion]

    @property
    def passed(self) -> list[Criterion]:
        return [c for c in self.criteria if c.verdict is Verdict.PASS]

    @property
    def failed(self) -> list[Criterion]:
        return [c for c in self.criteria if c.verdict is Verdict.FAIL]

    @property
    def blocked(self) -> list[Criterion]:
        """Criteria that cannot be decided yet — insufficient data or manual."""
        return [c for c in self.criteria if c.verdict in (Verdict.INSUFFICIENT, Verdict.MANUAL)]

    @property
    def ready(self) -> bool:
        """GA is granted only when every criterion is PASS.

        Deliberately strict: `INSUFFICIENT` never counts as ready. The whole
        point of the gate is to stop "we didn't measure it" from reading the
        same as "it's fine".
        """
        return bool(self.criteria) and all(c.verdict is Verdict.PASS for c in self.criteria)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "window_days": self.window_days,
            "ready": self.ready,
            "thresholds": self.thresholds,
            "summary": {
                "pass": len(self.passed),
                "fail": len(self.failed),
                "blocked": len(self.blocked),
                "total": len(self.criteria),
            },
            "criteria": [c.to_dict() for c in self.criteria],
        }


# ── Helpers ──────────────────────────────────────────────────────────


def _cutoff(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


async def _rows(settings: Settings | Any, sql: str, params: tuple[Any, ...]) -> list[aiosqlite.Row]:
    """Query, returning [] when the table does not exist yet."""
    try:
        async with aiosqlite.connect(str(settings.db_path)) as conn:
            conn.row_factory = aiosqlite.Row
            return list(await conn.execute_fetchall(sql, params))
    except aiosqlite.OperationalError:
        return []
    except Exception:
        logger.exception("GA gate query failed: %s", sql.split()[0:3])
        return []


# ── Criteria ─────────────────────────────────────────────────────────


async def call_volume_and_success(settings: Settings | Any, days: int, t: GAThresholds) -> list[Criterion]:
    """≥200 production calls, ≥95% success excluding no-answer, zero stuck."""
    rows = await _rows(
        settings,
        "SELECT failure_code FROM voice_calls WHERE ended_at IS NOT NULL AND started_at >= ?",
        (_cutoff(days),),
    )
    excluded = {str(c) for c in EXCLUDED_FROM_SLO}

    total = len(rows)
    eligible = 0
    completed = 0
    stuck = 0
    by_code: dict[str, int] = {}
    for row in rows:
        code = str(row["failure_code"] or FailureCode.NONE)
        by_code[code] = by_code.get(code, 0) + 1
        if code == FailureCode.STUCK:
            stuck += 1
        if code in excluded:
            continue
        eligible += 1
        if code == FailureCode.NONE:
            completed += 1

    criteria: list[Criterion] = []

    # 1. Volume — the criterion the whole pilot exists to satisfy.
    criteria.append(
        Criterion(
            key="call_volume",
            title=f"≥ {t.min_calls} production calls",
            verdict=Verdict.PASS if total >= t.min_calls else Verdict.INSUFFICIENT,
            summary=f"{total} terminated calls in the last {days} days",
            evidence={"calls": total, "required": t.min_calls, "by_failure_code": by_code},
            needed="" if total >= t.min_calls else f"{t.min_calls - total} more real calls",
        )
    )

    # 2. Success rate.
    rate = (completed / eligible) if eligible else None
    if eligible < t.min_calls:
        verdict, needed = Verdict.INSUFFICIENT, f"{t.min_calls - eligible} more eligible calls"
    elif rate is not None and rate >= t.call_success_min:
        verdict, needed = Verdict.PASS, ""
    else:
        verdict, needed = Verdict.FAIL, "fix the dominant failure codes, then re-measure"
    criteria.append(
        Criterion(
            key="call_success_rate",
            title=f"Call success ≥ {t.call_success_min:.0%} (excl. callee-unreachable)",
            verdict=verdict,
            summary=(
                f"{completed}/{eligible} eligible calls completed"
                + (f" ({rate:.1%})" if rate is not None else " (no data)")
            ),
            evidence={
                "completed": completed,
                "eligible": eligible,
                "excluded_callee_or_policy": total - eligible,
                "rate": round(rate, 4) if rate is not None else None,
                "required": t.call_success_min,
            },
            needed=needed,
        )
    )

    # 3. Stuck calls — one is a failure, not a percentage.
    criteria.append(
        Criterion(
            key="zero_stuck_calls",
            title="Zero stuck calls",
            verdict=Verdict.PASS
            if (stuck <= t.max_stuck_calls and total > 0)
            else (Verdict.INSUFFICIENT if total == 0 else Verdict.FAIL),
            summary=f"{stuck} call(s) reaped as stuck over {total} calls",
            evidence={"stuck": stuck, "calls": total},
            needed="" if stuck <= t.max_stuck_calls else "runbook §stuck-call: find the cause, not just the symptom",
        )
    )
    return criteria


async def booking_success(settings: Settings | Any, days: int, t: GAThresholds) -> Criterion:
    """≥80% on cooperative callees, ≥70% overall for appointment calls."""
    rows = await _rows(
        settings,
        "SELECT result FROM appointment_outcomes WHERE recorded_at >= ?",
        (_cutoff(days),),
    )
    by_result: dict[str, int] = {}
    for row in rows:
        result = str(row["result"] or "unknown")
        by_result[result] = by_result.get(result, 0) + 1

    overall_total = sum(by_result.values())
    cooperative = sum(n for r, n in by_result.items() if r not in ("unreachable", "voicemail"))
    confirmed = by_result.get("confirmed", 0)

    coop_rate = (confirmed / cooperative) if cooperative else None
    overall_rate = (confirmed / overall_total) if overall_total else None

    if overall_total < t.min_bookings:
        verdict = Verdict.INSUFFICIENT
        needed = f"{t.min_bookings - overall_total} more appointment calls"
    elif (
        coop_rate is not None
        and overall_rate is not None
        and coop_rate >= t.booking_cooperative_min
        and overall_rate >= t.booking_overall_min
    ):
        verdict, needed = Verdict.PASS, ""
    else:
        verdict = Verdict.FAIL
        needed = "review declined/out_of_slots transcripts; `calendar_failed` is our bug, not a negotiation loss"

    return Criterion(
        key="booking_success",
        title=f"Booking ≥ {t.booking_cooperative_min:.0%} cooperative / ≥ {t.booking_overall_min:.0%} overall",
        verdict=verdict,
        summary=(
            f"{confirmed}/{cooperative} cooperative"
            + (f" ({coop_rate:.0%})" if coop_rate is not None else "")
            + f", {confirmed}/{overall_total} overall"
            + (f" ({overall_rate:.0%})" if overall_rate is not None else "")
        ),
        evidence={
            "confirmed": confirmed,
            "cooperative_attempts": cooperative,
            "total_attempts": overall_total,
            "cooperative_rate": round(coop_rate, 4) if coop_rate is not None else None,
            "overall_rate": round(overall_rate, 4) if overall_rate is not None else None,
            "by_result": by_result,
            "required_cooperative": t.booking_cooperative_min,
            "required_overall": t.booking_overall_min,
        },
        needed=needed,
    )


def latency_per_language(settings: Settings | Any, days: int, t: GAThresholds) -> Criterion:
    """p50 ≤ 1.2s and p95 ≤ 2.0s, held in EVERY required language.

    Per-language on purpose: a DACH product whose German latency is twice its
    English latency has not met this bar, and an aggregate would hide it.
    """
    from pincer.observability.golden_signals import percentile
    from pincer.voice.latency_report import read_turn_records

    data_dir = getattr(settings, "data_dir", None)
    records: list[dict[str, Any]] = []
    if data_dir is not None:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        for record in read_turn_records(data_dir / "logs" / "voice_latency.jsonl", last_calls=5000):
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
            if isinstance(record.get("total_ms"), int | float):
                records.append(record)

    per_language: dict[str, list[float]] = {}
    for record in records:
        language = str(record.get("language") or "unknown")[:2] or "unknown"
        per_language.setdefault(language, []).append(float(record["total_ms"]) / 1000.0)

    evidence: dict[str, Any] = {"required_p50_s": t.latency_p50_max_s, "required_p95_s": t.latency_p95_max_s}
    missing: list[str] = []
    breached: list[str] = []

    for language in t.required_languages:
        values = per_language.get(language, [])
        p50 = percentile(values, 0.50)
        p95 = percentile(values, 0.95)
        evidence[language] = {
            "turns": len(values),
            "p50_s": round(p50, 3) if p50 is not None else None,
            "p95_s": round(p95, 3) if p95 is not None else None,
        }
        if len(values) < t.min_turns_per_language:
            missing.append(f"{language} ({len(values)}/{t.min_turns_per_language} turns)")
            continue
        if (p50 is not None and p50 > t.latency_p50_max_s) or (p95 is not None and p95 > t.latency_p95_max_s):
            breached.append(language)

    # Languages outside the required set are reported but never gate.
    for language, values in per_language.items():
        if language not in t.required_languages:
            evidence.setdefault("other_languages", {})[language] = {"turns": len(values)}

    if missing:
        verdict = Verdict.INSUFFICIENT
        summary = "Not enough turns in: " + ", ".join(missing)
        needed = "more pilot calls in the missing language(s)"
    elif breached:
        verdict = Verdict.FAIL
        summary = "Latency bar missed in: " + ", ".join(breached)
        needed = "`pincer voice latency-report` to find the regressed stage; runbook §latency-regression"
    else:
        verdict = Verdict.PASS
        summary = " · ".join(
            f"{lang} p50 {evidence[lang]['p50_s']}s / p95 {evidence[lang]['p95_s']}s" for lang in t.required_languages
        )
        needed = ""

    return Criterion(
        key="latency",
        title=f"p50 ≤ {t.latency_p50_max_s}s / p95 ≤ {t.latency_p95_max_s}s, per language",
        verdict=verdict,
        summary=summary,
        evidence=evidence,
        needed=needed,
    )


async def cost_per_call(settings: Settings | Any, days: int, t: GAThresholds) -> Criterion:
    """Actual cost per call vs. the $0.12–0.25 model.

    Being *under* the model is reported, not failed — cheaper than planned is
    good news that still needs to reach whoever sets the price. Only overshoot
    fails, since that is the number the pricing decision depends on.
    """
    rows = await _rows(
        settings,
        "SELECT total_usd, twilio_usd, stt_usd, tts_usd, llm_usd FROM call_costs WHERE recorded_at >= ?",
        (_cutoff(days),),
    )
    totals = [float(r["total_usd"] or 0.0) for r in rows]

    if len(totals) < t.min_priced_calls:
        return Criterion(
            key="cost_per_call",
            title=f"Cost per call within ${t.cost_per_call_min_usd:.2f}–${t.cost_per_call_max_usd:.2f}",
            verdict=Verdict.INSUFFICIENT,
            summary=f"only {len(totals)} priced calls",
            evidence={"priced_calls": len(totals), "required": t.min_priced_calls},
            needed=f"{t.min_priced_calls - len(totals)} more priced calls",
        )

    from pincer.observability.golden_signals import percentile

    mean = sum(totals) / len(totals)
    p95 = percentile(totals, 0.95)
    components = {
        name: round(sum(float(r[f"{name}_usd"] or 0.0) for r in rows), 4) for name in ("twilio", "stt", "tts", "llm")
    }
    spend = round(sum(totals), 2)

    over = mean > t.cost_per_call_max_usd
    return Criterion(
        key="cost_per_call",
        title=f"Cost per call within ${t.cost_per_call_min_usd:.2f}–${t.cost_per_call_max_usd:.2f}",
        verdict=Verdict.FAIL if over else Verdict.PASS,
        summary=(
            f"mean ${mean:.3f}/call, p95 ${p95:.3f} over {len(totals)} calls (${spend:.2f} total)"
            + (" — ABOVE model" if over else (" — below model" if mean < t.cost_per_call_min_usd else ""))
        ),
        evidence={
            "mean_usd": round(mean, 4),
            "p95_usd": round(p95, 4) if p95 else None,
            "total_spend_usd": spend,
            "priced_calls": len(totals),
            "by_component_usd": components,
            "model_min": t.cost_per_call_min_usd,
            "model_max": t.cost_per_call_max_usd,
            "note": "Accuracy depends on PINCER_PRICE_* matching your real account rates.",
        },
        needed="" if not over else "runbook §cost-spike: identify the dominant component before repricing",
    )


def security_findings(settings: Settings | Any) -> Criterion:
    """Zero unresolved security findings — the Sprint 8 gate, re-run now."""
    try:
        from pincer.security.doctor import CheckStatus, SecurityDoctor

        report = SecurityDoctor(production=True).run_all()
    except Exception as e:
        return Criterion(
            key="security",
            title="Zero unresolved security findings",
            verdict=Verdict.INSUFFICIENT,
            summary=f"doctor could not run: {e}",
            needed="fix `pincer doctor --production` so it can be evaluated",
        )

    critical = [c for c in report.checks if c.status == CheckStatus.CRITICAL]
    warnings = [c for c in report.checks if c.status == CheckStatus.WARNING]
    return Criterion(
        key="security",
        title="Zero unresolved security findings",
        verdict=Verdict.PASS if not critical else Verdict.FAIL,
        summary=f"{len(critical)} CRITICAL, {len(warnings)} warning(s) from `doctor --production`",
        evidence={
            "critical": [{"name": c.name, "message": c.message} for c in critical],
            "warnings": [c.name for c in warnings],
            "score": report.score,
            "checklist": "docs/guides/security-checklist.md",
        },
        needed="" if not critical else "resolve every CRITICAL and re-run; findings table in the security checklist",
    )


async def _count_blocked_dials(settings: Settings | Any, days: int) -> int:
    """Dials the abuse gate refused, read straight from audit.db.

    Deliberately NOT via `get_audit_logger()`: that singleton owns a batched
    writer and a long-lived connection which never gets shut down here, and a
    one-shot CLI invocation would hang on exit waiting for it.
    """
    audit_db = getattr(settings, "data_dir", None)
    if audit_db is None:
        return 0
    try:
        async with aiosqlite.connect(str(audit_db / "audit.db")) as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE action = ? AND timestamp >= ?",
                ("voice_call_blocked", _cutoff(days)),
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0
    except aiosqlite.OperationalError:
        return 0
    except Exception:
        logger.debug("Blocked-dial audit count failed", exc_info=True)
        return 0


async def compliance_incidents(settings: Settings | Any, days: int) -> Criterion:
    """Zero compliance incidents: consent, quiet hours, and opt-out all clean.

    "Clean" here means the guardrails *engaged correctly*, not that they never
    engaged. A blocked quiet-hours call is the system working; a call placed
    during quiet hours, or to a do-not-call number, is the incident.
    """
    settings_ok: list[str] = []
    problems: list[str] = []

    if str(getattr(settings, "voice_consent_mode", "")) == "two_party":
        settings_ok.append("two-party consent")
    else:
        problems.append(f"consent_mode={getattr(settings, 'voice_consent_mode', 'unset')} (DACH requires two_party)")

    from pincer.voice.safety_gates import parse_quiet_hours

    if parse_quiet_hours(str(getattr(settings, "voice_quiet_hours", "") or "")) is not None:
        settings_ok.append("quiet hours")
    else:
        problems.append("quiet hours disabled")

    # Did any call actually go out to a do-not-call number? The gate blocks
    # them, so a violation means something reached Twilio around the gate.
    violations = await _rows(
        settings,
        "SELECT o.phone_number, o.placed_at FROM outbound_call_log o "
        "JOIN do_not_call d ON d.phone_number = o.phone_number "
        "WHERE o.placed_at >= ? AND o.placed_at > d.added_at",
        (_cutoff(days),),
    )

    blocked = await _count_blocked_dials(settings, days)

    if violations:
        verdict = Verdict.FAIL
        summary = f"{len(violations)} call(s) placed to a do-not-call number — the gate was bypassed"
        needed = "find the dial path that skipped voice.safety_gates.check_outbound_allowed"
    elif problems:
        verdict = Verdict.FAIL
        summary = "Compliance settings weakened: " + "; ".join(problems)
        needed = "restore the DACH defaults (see .env.production.example)"
    else:
        verdict = Verdict.PASS
        summary = f"{', '.join(settings_ok)} active; {blocked} dial(s) correctly blocked; no opt-out violations"
        needed = ""

    return Criterion(
        key="compliance",
        title="Zero compliance incidents (consent, quiet hours, opt-out)",
        verdict=verdict,
        summary=summary,
        evidence={
            "consent_mode": str(getattr(settings, "voice_consent_mode", "")),
            "quiet_hours": str(getattr(settings, "voice_quiet_hours", "")),
            "blocked_dials": blocked,
            "do_not_call_violations": len(violations),
            "retention_days": int(getattr(settings, "voice_transcript_retention_days", 0) or 0),
        },
        needed=needed,
    )


async def alert_quality(settings: Settings | Any, days: int) -> Criterion:
    """Every alert that fired was actionable — a human judgement, pre-loaded.

    The tool cannot know whether an alert was useful, so it does the part it
    can: list what fired, so the review argues about a real list instead of
    a memory.
    """
    # Alert deliveries are not persisted (suppression state is in-process by
    # design), so the durable record of alert-worthy events is what the
    # database kept: failed canary runs and reaped calls.
    fired: dict[str, int] = {}
    canary_failures = await _rows(
        settings, "SELECT reason FROM canary_runs WHERE ran_at >= ? AND ok = 0", (_cutoff(days),)
    )
    if canary_failures:
        fired["canary_failed"] = len(canary_failures)

    stuck = await _rows(
        settings,
        "SELECT call_sid FROM voice_calls WHERE failure_code = 'stuck' AND started_at >= ?",
        (_cutoff(days),),
    )
    if stuck:
        fired["stuck_calls"] = len(stuck)

    return Criterion(
        key="alert_quality",
        title="All alerts that fired were actionable; runbook gaps closed",
        verdict=Verdict.MANUAL,
        summary=(
            f"{sum(fired.values())} recorded alert-worthy event(s): {fired}"
            if fired
            else "No alert-worthy events recorded in the window"
        ),
        evidence={"events": fired, "window_days": days},
        needed=(
            "Review each firing with the on-call person: was it actionable, did the runbook section resolve it, "
            "and was a gap closed? Record the answers in the gate minutes."
        ),
    )


def pilot_feedback() -> Criterion:
    """NPS, testimonials, churn risk, and the callee-side quality audit."""
    return Criterion(
        key="pilot_feedback",
        title="Pilot NPS / testimonials collected; churn risk assessed",
        verdict=Verdict.MANUAL,
        summary="Human input required",
        evidence={},
        needed=(
            "Per pilot: NPS score, one usable testimonial (or an explicit decline), churn-risk rating with a reason, "
            "and the callee-side audit result — ≥80% of sampled callees answering 'pleasant', plus whether they "
            "realised they were talking to an AI (disclosure working). Template: docs/operations/pilot-feedback.md"
        ),
    )


# ── Assembly ─────────────────────────────────────────────────────────


async def evaluate(
    settings: Settings | Any,
    days: int = 14,
    thresholds: GAThresholds | None = None,
) -> GAGateReport:
    """Run every GA criterion over the pilot window."""
    t = thresholds or GAThresholds()
    criteria: list[Criterion] = []
    criteria.extend(await call_volume_and_success(settings, days, t))
    criteria.append(await booking_success(settings, days, t))
    criteria.append(latency_per_language(settings, days, t))
    criteria.append(security_findings(settings))
    criteria.append(await compliance_incidents(settings, days))
    criteria.append(await cost_per_call(settings, days, t))
    criteria.append(await alert_quality(settings, days))
    criteria.append(pilot_feedback())

    return GAGateReport(
        generated_at=datetime.now(UTC).isoformat(),
        window_days=days,
        thresholds=asdict(t),
        criteria=criteria,
    )


_ICONS = {
    Verdict.PASS: "✅",
    Verdict.FAIL: "❌",
    Verdict.INSUFFICIENT: "⏳",
    Verdict.MANUAL: "🧑",
}


def render_markdown(report: GAGateReport) -> str:
    """The sign-off document. Paste into the meeting minutes verbatim."""
    lines = [
        "# GA Gate Review",
        "",
        f"Generated: {report.generated_at}  ",
        f"Window: last {report.window_days} days",
        "",
        (
            "## Verdict: ✅ READY FOR GA"
            if report.ready
            else f"## Verdict: 🚫 NOT READY — {len(report.failed)} failed, {len(report.blocked)} undecided"
        ),
        "",
        f"{len(report.passed)}/{len(report.criteria)} criteria met.",
        "",
        "| | Criterion | Result |",
        "|---|---|---|",
    ]
    for c in report.criteria:
        lines.append(f"| {_ICONS[c.verdict]} | {c.title} | {c.summary} |")

    lines.extend(["", "## Detail", ""])
    for c in report.criteria:
        lines.append(f"### {_ICONS[c.verdict]} {c.title}")
        lines.append("")
        lines.append(f"**{c.verdict.value}** — {c.summary}")
        if c.needed:
            lines.extend(["", f"**Needed:** {c.needed}"])
        if c.evidence:
            lines.extend(["", "```json", _pretty(c.evidence), "```"])
        lines.append("")

    if not report.ready:
        lines.extend(
            [
                "## Decision required",
                "",
                "Each item below is either fixed before GA, or granted a **dated exception with a named owner**.",
                "An undecided criterion is not an exception — it is a measurement that has not been taken.",
                "",
                "| Criterion | Verdict | Owner | Decision | Review by |",
                "|---|---|---|---|---|",
            ]
        )
        for c in report.failed + report.blocked:
            lines.append(f"| {c.title} | {c.verdict.value} | `<name>` | fix / exception | `<date>` |")
        lines.append("")

    return "\n".join(lines)


def _pretty(data: dict[str, Any]) -> str:
    import json

    return json.dumps(data, indent=2, ensure_ascii=False, default=str)
