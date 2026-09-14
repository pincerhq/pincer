"""
Telephony telemetry API — the internal engineering dashboard's backend.

Access control: the whole `/api/*` surface is behind the dashboard bearer token
(`api/server.py` middleware), which is what "internal dashboard permissions"
means in this deployment. On top of that, every query here is tenant-scoped and
**fails closed**: a caller restricted to a set of tenants can only ever read
those tenants' calls, and a caller restricted to an empty set reads nothing.

Content policy: these endpoints serve *technical* telemetry. Phone numbers are
already masked at write time, and transcripts, recordings, prompts, tool
arguments and tool results never enter these tables at all — they stay behind
the existing `/api/voice/calls/{sid}` surface with its own permissions and its
own (shorter) retention.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from pincer.config import get_settings_relaxed
from pincer.voice.telemetry import alerts as telephony_alerts
from pincer.voice.telemetry import queries, runtime
from pincer.voice.telemetry.outcomes import DENOMINATORS, FailureCategory
from pincer.voice.telemetry.schema import METRICS

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/telephony", tags=["telephony"])

#: Request header a tenancy-aware deployment sets. Absent -> unrestricted,
#: which is the correct answer for a single-tenant install.
TENANT_HEADER = "X-Pincer-Tenant"


def _db_path() -> Path:
    return get_settings_relaxed().db_path


def _scope(request: Request) -> queries.TenantScope:
    """Resolve which tenants this request may read.

    Precedence, most trusted first:

    1. ``request.state.tenant_ids`` — set by an authenticating middleware in a
       tenancy-aware deployment. Authoritative.
    2. The ``X-Pincer-Tenant`` header, intersected with (1) when present. On its
       own it is a *narrowing* convenience, never a grant: a header cannot widen
       what the middleware allowed.
    3. Nothing — unrestricted, the single-tenant case.
    """
    allowed = getattr(request.state, "tenant_ids", None)
    header = (request.headers.get(TENANT_HEADER) or "").strip()
    if allowed is None:
        return queries.TenantScope(allowed=(header,) if header else None)
    permitted = tuple(str(t) for t in allowed)
    if header:
        if header not in permitted:
            raise HTTPException(status_code=403, detail="Tenant not permitted")
        return queries.TenantScope(allowed=(header,))
    return queries.TenantScope(allowed=permitted)


def _filters(
    request: Request,
    hours: float,
    environment: str,
    app_version: str,
    direction: str,
    provider: str,
    engine: str,
    model: str,
    language: str,
    status: str,
    failure_category: str,
    failure_code: str,
    search: str,
) -> queries.CallFilters:
    return queries.CallFilters.for_hours(
        hours,
        environment=environment,
        app_version=app_version,
        tenant_id="",
        direction=direction,
        provider=provider,
        engine=engine,
        model=model,
        language=language,
        status=status,
        failure_category=failure_category,
        failure_code=failure_code,
        search=search,
    )


class MetricDefinitionOut(BaseModel):
    key: str
    label: str
    start_event: str
    end_event: str
    source: str
    unit: str
    available_on: list[str]
    limitations: str
    unavailable_reason: str


class HealthOut(BaseModel):
    enabled: bool
    sample_rate: float
    active_calls_traced: int
    coverage: float
    healthy: bool
    export: dict[str, Any] = Field(default_factory=dict)


class CallListOut(BaseModel):
    total: int
    limit: int
    offset: int
    calls: list[dict[str, Any]]


class CallDetailOut(BaseModel):
    call: dict[str, Any]
    turns: list[dict[str, Any]]
    metrics: list[MetricDefinitionOut]
    unavailable: list[MetricDefinitionOut]
    telemetry_gaps: list[str]


# ── definitions and health ───────────────────────────────────────────


@router.get("/metrics", response_model=list[MetricDefinitionOut])
async def metric_definitions() -> list[MetricDefinitionOut]:
    """Every latency metric with its exact boundaries and limitations.

    Served rather than documented-only so the UI can show the definition next
    to the number; a latency without its boundaries is a rumour.
    """
    return [MetricDefinitionOut(**m.to_dict()) for m in METRICS.values()]  # type: ignore[arg-type]


@router.get("/health", response_model=HealthOut)
async def telemetry_health() -> HealthOut:
    """Exporter health: dropped records and export failures are first-class.

    A dashboard that cannot say whether it is still receiving data is worse
    than no dashboard, because silence reads as success.
    """
    return HealthOut(**runtime.health())


@router.get("/denominators")
async def denominators() -> dict[str, str]:
    """What each rate is divided by. Shown next to the rate in the UI."""
    return DENOMINATORS


@router.get("/failure-categories")
async def failure_categories() -> list[dict[str, str]]:
    return [
        {"value": str(FailureCategory.NONE), "label": "Succeeded"},
        {"value": str(FailureCategory.TECHNICAL), "label": "Technical failure (ours)"},
        {"value": str(FailureCategory.CALLEE_UNAVAILABLE), "label": "Callee unavailable"},
        {"value": str(FailureCategory.POLICY_DECLINED), "label": "Declined by policy"},
        {"value": str(FailureCategory.ENDED_BY_PARTY), "label": "Ended by a party"},
        {"value": str(FailureCategory.UNKNOWN), "label": "Unclassified"},
    ]


# ── aggregate ────────────────────────────────────────────────────────


@router.get("/overview")
async def overview(
    request: Request,
    hours: float = Query(default=24.0, ge=0.1, le=8760.0),
    environment: str = "",
    app_version: str = "",
    direction: str = "",
    provider: str = "",
    engine: str = "",
    model: str = "",
    language: str = "",
    status: str = "",
    failure_category: str = "",
    failure_code: str = "",
    search: str = "",
) -> dict[str, Any]:
    """Counts, rates, per-stage percentiles, distributions, trends, coverage."""
    settings = get_settings_relaxed()
    filters = _filters(
        request,
        hours,
        environment,
        app_version,
        direction,
        provider,
        engine,
        model,
        language,
        status,
        failure_category,
        failure_code,
        search,
    )
    aggregate = await queries.overview(
        _db_path(),
        filters,
        scope=_scope(request),
        min_samples=int(getattr(settings, "telephony_min_samples", 20) or 20),
    )
    payload = aggregate.to_dict()
    payload["window_hours"] = hours
    payload["telemetry"] = runtime.health()
    return payload


@router.get("/calls", response_model=CallListOut)
async def list_calls(
    request: Request,
    hours: float = Query(default=24.0, ge=0.1, le=8760.0),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    sort: str = "registered_at",
    order: str = "desc",
    environment: str = "",
    app_version: str = "",
    direction: str = "",
    provider: str = "",
    engine: str = "",
    model: str = "",
    language: str = "",
    status: str = "",
    failure_category: str = "",
    failure_code: str = "",
    search: str = "",
) -> CallListOut:
    """Searchable, sortable, paginated call table."""
    filters = _filters(
        request,
        hours,
        environment,
        app_version,
        direction,
        provider,
        engine,
        model,
        language,
        status,
        failure_category,
        failure_code,
        search,
    )
    result = await queries.search_calls(
        _db_path(), filters, scope=_scope(request), limit=limit, offset=offset, sort=sort, order=order
    )
    return CallListOut(**result)


@router.get("/turns/slowest")
async def slowest_turns(
    request: Request,
    hours: float = Query(default=24.0, ge=0.1, le=8760.0),
    limit: int = Query(default=20, ge=1, le=200),
    engine: str = "",
    model: str = "",
    direction: str = "",
) -> list[dict[str, Any]]:
    """Slowest response turns in the window, each with its attributed bottleneck."""
    filters = queries.CallFilters.for_hours(hours, engine=engine, model=model, direction=direction)
    return await queries.slowest_turns(_db_path(), filters, scope=_scope(request), limit=limit)


@router.get("/alerts")
async def alert_state(request: Request) -> list[dict[str, Any]]:
    """Every configured rule with its current value, threshold and evidence.

    Non-firing rules are included on purpose: "the rule exists and is quiet" and
    "the rule was never configured" must not look the same.
    """
    _scope(request)  # enforce tenant permission before reading anything
    settings = get_settings_relaxed()
    evaluated = await telephony_alerts.evaluate(_db_path(), settings)
    return [a.to_dict() for a in evaluated]


# ── export ───────────────────────────────────────────────────────────

#: Datasets that can be downloaded, and the columns each one exports. Explicit
#: rather than "whatever the row has": an export is an interface, and a column
#: appearing or vanishing because a query changed shape breaks whatever consumes
#: it. Columns are also the review point for what leaves the system — note that
#: only masked numbers are here, and no content column exists to add.
EXPORT_COLUMNS: dict[str, tuple[str, ...]] = {
    "calls": (
        "call_id",
        "provider_call_id",
        "trace_id",
        "direction",
        "provider",
        "engine",
        "transport",
        "codec",
        "sample_rate",
        "model",
        "language",
        "tenant_id",
        "environment",
        "app_version",
        "from_number_masked",
        "to_number_masked",
        "registered_at",
        "answered_at",
        "ended_at",
        "status",
        "outcome",
        "failure_category",
        "failure_code",
        "termination_reason",
        "duration_ms",
        "setup_ms",
        "media_establish_ms",
        "turn_count",
        "tool_count",
        "error_count",
        "timeout_count",
        "retry_count",
        "interruption_count",
        "reconnect_count",
        "sampled",
        "coverage",
    ),
    "turns": (
        "turn_id",
        "call_id",
        "provider_call_id",
        "turn_no",
        "created_at",
        "engine",
        "model",
        "language",
        "response_latency_ms",
        "response_latency_source",
        "endpointing_ms",
        "stt_first_partial_ms",
        "stt_final_ms",
        "agent_queue_ms",
        "agent_prep_ms",
        "llm_ttft_ms",
        "llm_total_ms",
        "tool_total_ms",
        "tts_first_audio_ms",
        "tts_total_ms",
        "audio_queue_ms",
        "total_ms",
        "tool_calls",
        "tool_retries",
        "tool_timeouts",
        "interrupted",
        "cancelled",
        "error",
        "bottleneck_stage",
        "bottleneck_ms",
        "complete",
    ),
    "stages": ("stage", "p50_ms", "p95_ms", "p99_ms", "min_ms", "max_ms", "mean_ms", "samples", "sufficient_samples"),
}

#: A row cap, so a download cannot become a denial of service against the box
#: serving live calls. Exceeding it is reported in the filename, not silently.
EXPORT_ROW_LIMIT = 50_000


def _csv(columns: tuple[str, ...], rows: list[dict[str, Any]]) -> str:
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(columns), extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: _csv_value(row.get(column)) for column in columns})
    return buffer.getvalue()


def _csv_value(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else value


async def _export_rows(request: Request, dataset: str, filters: queries.CallFilters) -> list[dict[str, Any]]:
    scope = _scope(request)
    db = _db_path()
    if dataset == "calls":
        result = await queries.search_calls(db, filters, scope=scope, limit=EXPORT_ROW_LIMIT, offset=0)
        return list(result["calls"])
    if dataset == "turns":
        return await queries.export_turns(db, filters, scope=scope, limit=EXPORT_ROW_LIMIT)
    if dataset == "stages":
        settings = get_settings_relaxed()
        aggregate = await queries.overview(
            db, filters, scope=scope, min_samples=int(getattr(settings, "telephony_min_samples", 20) or 20)
        )
        return [
            {
                "stage": stage,
                "p50_ms": summary["p50"],
                "p95_ms": summary["p95"],
                "p99_ms": summary["p99"],
                "min_ms": summary["min"],
                "max_ms": summary["max"],
                "mean_ms": summary["mean"],
                "samples": summary["count"],
                "sufficient_samples": summary["sufficient_samples"],
            }
            for stage, summary in aggregate.stages.items()
        ]
    raise HTTPException(status_code=400, detail=f"Unknown dataset {dataset!r}")


@router.get("/export")
async def export_dataset(
    request: Request,
    dataset: str = Query(default="calls", pattern="^(calls|turns|stages)$"),
    fmt: str = Query(default="csv", alias="format", pattern="^(csv|json)$"),
    hours: float = Query(default=24.0, ge=0.1, le=8760.0),
    environment: str = "",
    app_version: str = "",
    direction: str = "",
    provider: str = "",
    engine: str = "",
    model: str = "",
    language: str = "",
    status: str = "",
    failure_category: str = "",
    failure_code: str = "",
    search: str = "",
) -> StreamingResponse:
    """Download the CURRENT FILTER's data as CSV or JSON.

    The whole filtered set, not the page on screen — an export that silently
    stops at the pagination boundary is worse than no export, because the
    spreadsheet it lands in looks complete.

    Tenant scoping and the content policy apply exactly as they do to the
    interactive endpoints: masked numbers only, and no transcript, audio, prompt
    or tool payload exists in these tables to export.
    """
    import json as json_lib

    filters = _filters(
        request,
        hours,
        environment,
        app_version,
        direction,
        provider,
        engine,
        model,
        language,
        status,
        failure_category,
        failure_code,
        search,
    )
    rows = await _export_rows(request, dataset, filters)
    truncated = len(rows) >= EXPORT_ROW_LIMIT
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"telephony-{dataset}-{stamp}{'-truncated' if truncated else ''}.{fmt}"

    if fmt == "json":
        body = json_lib.dumps({"dataset": dataset, "rows": rows, "truncated": truncated}, default=str, indent=2)
        media_type = "application/json"
    else:
        body = _csv(EXPORT_COLUMNS[dataset], rows)
        media_type = "text/csv"

    return StreamingResponse(
        iter([body]),
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Export-Rows": str(len(rows)),
            "X-Export-Truncated": "true" if truncated else "false",
        },
    )


# ── per call ─────────────────────────────────────────────────────────


async def _resolve_call(request: Request, call_ref: str) -> dict[str, Any]:
    call = await queries.get_call(_db_path(), call_ref, scope=_scope(request))
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")
    return call


@router.get("/calls/{call_ref}", response_model=CallDetailOut)
async def call_detail(request: Request, call_ref: str) -> CallDetailOut:
    """One call by internal id or provider CallSid, with its turn breakdown.

    `telemetry_gaps` names what is missing and why, so a sparse page reads as a
    known limitation rather than a broken one.
    """
    call = await _resolve_call(request, call_ref)
    turns = await queries.get_turns(_db_path(), call["call_id"])
    engine = str(call.get("engine") or "")
    available = [m for m in METRICS.values() if m.available(engine)]
    unavailable = [m for m in METRICS.values() if not m.available(engine)]

    gaps: list[str] = []
    if not call.get("sampled", True):
        gaps.append("This call was not sampled: lifecycle events were recorded, turn-level detail was not.")
    if str(call.get("coverage") or "full") != "full":
        gaps.append(f"Telemetry coverage for this call is `{call.get('coverage')}` rather than complete.")
    if call.get("status") == "active":
        gaps.append("The call is still in progress; its totals are provisional.")
    if not turns and int(call.get("turn_count") or 0) > 0:
        gaps.append("Turn rows are missing although turns were counted — export was likely dropping records.")
    if not call.get("answered_at"):
        gaps.append("The call never connected, so no conversation telemetry exists for it.")

    return CallDetailOut(
        call=call,
        turns=turns,
        metrics=[MetricDefinitionOut(**m.to_dict()) for m in available],  # type: ignore[arg-type]
        unavailable=[MetricDefinitionOut(**m.to_dict()) for m in unavailable],  # type: ignore[arg-type]
        telemetry_gaps=gaps,
    )


@router.get("/calls/{call_ref}/events")
async def call_events(
    request: Request,
    call_ref: str,
    limit: int = Query(default=2000, ge=1, le=20000),
) -> list[dict[str, Any]]:
    """Chronological event timeline, ordered by (ts_utc, seq).

    Ordering is applied at read time, so a Twilio status callback that arrives
    minutes late still lands where it belongs.
    """
    call = await _resolve_call(request, call_ref)
    return await queries.get_events(_db_path(), call["call_id"], limit=limit)


@router.get("/calls/{call_ref}/spans")
async def call_spans(
    request: Request,
    call_ref: str,
    limit: int = Query(default=2000, ge=1, le=20000),
) -> list[dict[str, Any]]:
    """Waterfall spans. They overlap — that is the pipeline, not a bug."""
    call = await _resolve_call(request, call_ref)
    return await queries.get_spans(_db_path(), call["call_id"], limit=limit)


@router.get("/calls/{call_ref}/turns")
async def call_turns(request: Request, call_ref: str) -> list[dict[str, Any]]:
    call = await _resolve_call(request, call_ref)
    return await queries.get_turns(_db_path(), call["call_id"])
