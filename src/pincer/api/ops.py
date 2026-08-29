"""Voice Ops API (Sprint 9) — everything the Voice Ops dashboard renders.

`/api/ops/*` sits behind the same bearer auth as the rest of `/api/*`. It is
deliberately read-only apart from `POST /api/ops/canary`, which an operator uses
to prove the media path works *right now* rather than waiting for the next
scheduled run.

The golden signals are computed from SQLite here rather than proxied from the
metrics backend, for the same reason the alert rules are: this page has to work
when the monitoring stack is the thing that is broken.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from pincer.config import get_settings_relaxed

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ops", tags=["ops"])


class SignalOut(BaseModel):
    name: str
    value: float | None = None
    unit: str = ""
    sample_size: int = 0
    min_sample: int = 1
    target: float | None = None
    window: str = ""
    sufficient_data: bool = False
    detail: dict[str, Any] = Field(default_factory=dict)


class GoldenSignalsOut(BaseModel):
    generated_at: str
    signals: dict[str, SignalOut]


class AlertOut(BaseModel):
    rule: str
    severity: str
    title: str
    detail: str
    value: float | None = None
    threshold: float | None = None
    runbook: str = ""


class SLOOut(BaseModel):
    name: str
    target: float
    actual: float | None = None
    unit: str = ""
    window: str = ""
    sample_size: int = 0
    budget_total: float | None = None
    budget_spent: float | None = None
    burn_pct: float | None = None
    met: bool | None = None
    confidence: str = "measured"
    detail: dict[str, Any] = Field(default_factory=dict)


class SLOReportOut(BaseModel):
    generated_at: str
    freeze_threshold_pct: float
    freeze_min_sample: int
    feature_freeze: bool
    freeze_reason: str = ""
    slos: list[SLOOut] = Field(default_factory=list)


class CanaryRunOut(BaseModel):
    ran_at: str
    ok: bool
    skipped: bool = False
    reason: str = ""
    call_sid: str = ""
    turns: int = 0
    duration_s: float = 0.0


class CanaryTriggerOut(BaseModel):
    ok: bool
    skipped: bool
    reason: str = ""
    call_sid: str = ""
    turns: int = 0
    duration_s: float = 0.0


class FailureBreakdownOut(BaseModel):
    window_hours: float
    total: int
    codes: list[dict[str, Any]] = Field(default_factory=list)


@router.get("/signals", response_model=GoldenSignalsOut)
async def golden_signals_endpoint() -> GoldenSignalsOut:
    """The five golden signals, each in its configured window."""
    from pincer.observability.golden_signals import collect

    payload = (await collect(get_settings_relaxed())).to_dict()
    return GoldenSignalsOut(
        generated_at=payload["generated_at"],
        signals={name: SignalOut(**data) for name, data in payload["signals"].items()},
    )


@router.get("/alerts", response_model=list[AlertOut])
async def current_alerts() -> list[AlertOut]:
    """Alerts that would fire right now. Evaluated, not delivered — opening the
    dashboard must never send anyone a notification."""
    from pincer.observability import golden_signals as gs
    from pincer.observability.alerts import disk_alert, evaluate

    settings = get_settings_relaxed()
    signals = await gs.collect(settings)
    alerts = evaluate(signals, settings)
    host = disk_alert(settings)
    if host is not None:
        alerts.insert(0, host)
    return [
        AlertOut(
            rule=a.rule,
            severity=str(a.severity),
            title=a.title,
            detail=a.detail,
            value=a.value,
            threshold=a.threshold,
            runbook=a.runbook,
        )
        for a in alerts
    ]


@router.get("/slo", response_model=SLOReportOut)
async def slo_report() -> SLOReportOut:
    """Month-to-date SLO status and error-budget burn."""
    from pincer.observability.slo import collect

    payload = await collect(get_settings_relaxed())
    return SLOReportOut(
        generated_at=payload["generated_at"],
        freeze_threshold_pct=payload["freeze_threshold_pct"],
        freeze_min_sample=payload["freeze_min_sample"],
        feature_freeze=payload["feature_freeze"],
        freeze_reason=payload["freeze_reason"],
        slos=[SLOOut(**s) for s in payload["slos"]],
    )


@router.get("/failures", response_model=FailureBreakdownOut)
async def failure_breakdown(hours: float = Query(default=168.0, ge=1, le=8760)) -> FailureBreakdownOut:
    """Failure codes over a window, ranked, with their human descriptions."""
    from pincer.observability.failure_codes import describe
    from pincer.observability.golden_signals import call_success_rate

    signal = await call_success_rate(get_settings_relaxed(), window_hours=hours)
    by_code = signal.detail.get("by_failure_code") or {}
    codes = [
        {"code": code, "count": count, "description": describe(code)}
        for code, count in sorted(by_code.items(), key=lambda kv: kv[1], reverse=True)
    ]
    return FailureBreakdownOut(window_hours=hours, total=signal.sample_size, codes=codes)


@router.get("/canary", response_model=list[CanaryRunOut])
async def canary_history(limit: int = Query(default=20, ge=1, le=200)) -> list[CanaryRunOut]:
    from pincer.observability.canary import recent_runs

    runs = await recent_runs(get_settings_relaxed(), limit=limit)
    return [
        CanaryRunOut(
            ran_at=str(r.get("ran_at", "")),
            ok=bool(r.get("ok")),
            skipped=bool(r.get("skipped")),
            reason=str(r.get("reason") or ""),
            call_sid=str(r.get("call_sid") or ""),
            turns=int(r.get("turns") or 0),
            duration_s=float(r.get("duration_s") or 0.0),
        )
        for r in runs
    ]


@router.post("/canary", response_model=CanaryTriggerOut, status_code=202)
async def trigger_canary() -> CanaryTriggerOut:
    """Run the synthetic canary now.

    Places a real phone call, so it refuses unless the canary is configured —
    an operator clicking a button must not be able to dial an unset number, and
    the Sprint 8 abuse gate still applies underneath.
    """
    settings = get_settings_relaxed()
    if not getattr(settings, "voice_canary_enabled", False):
        raise HTTPException(status_code=409, detail="Canary is disabled (PINCER_VOICE_CANARY_ENABLED=false)")
    if not str(getattr(settings, "voice_canary_number", "") or "").strip():
        raise HTTPException(status_code=409, detail="PINCER_VOICE_CANARY_NUMBER is not set")

    from pincer.observability.canary import run_and_alert

    result = await run_and_alert(settings)
    return CanaryTriggerOut(
        ok=result.ok,
        skipped=result.skipped,
        reason=result.reason,
        call_sid=result.call_sid,
        turns=result.turns,
        duration_s=round(result.duration_s, 2),
    )


@router.get("/digest")
async def weekly_digest() -> dict[str, str]:
    """The weekly digest text, rendered on demand (does not send it anywhere)."""
    from pincer.observability.digest import build_digest

    return {"digest": await build_digest(get_settings_relaxed())}


# ── GA gate (Sprint 10, T10.4) ───────────────────────────────────────


class GACriterionOut(BaseModel):
    key: str
    title: str
    verdict: str
    summary: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    needed: str = ""


class GAGateOut(BaseModel):
    generated_at: str
    window_days: int
    ready: bool
    summary: dict[str, int]
    thresholds: dict[str, Any]
    criteria: list[GACriterionOut]


@router.get("/ga-gate", response_model=GAGateOut)
async def ga_gate(days: int = Query(default=14, ge=1, le=365)) -> GAGateOut:
    """The GA exit criteria evaluated against real data.

    `ready` is true only when every criterion passes — insufficient data never
    counts as a pass. Runs `doctor --production`, which probes configured voice
    providers, so this endpoint is slower than the rest of /api/ops.
    """
    from pincer.observability.ga_gate import evaluate

    report = await evaluate(get_settings_relaxed(), days=days)
    payload = report.to_dict()
    return GAGateOut(
        generated_at=payload["generated_at"],
        window_days=payload["window_days"],
        ready=payload["ready"],
        summary=payload["summary"],
        thresholds=payload["thresholds"],
        criteria=[GACriterionOut(**c) for c in payload["criteria"]],
    )


@router.get("/ga-gate/report")
async def ga_gate_markdown(days: int = Query(default=14, ge=1, le=365)) -> dict[str, str]:
    """The sign-off document, rendered as markdown."""
    from pincer.observability.ga_gate import evaluate, render_markdown

    return {"report": render_markdown(await evaluate(get_settings_relaxed(), days=days))}
