"""
Operational alerts over telephony telemetry.

Every rule has the same four knobs — threshold, evaluation window, minimum
sample size, and enabled — because a pilot doing five calls a day and a
customer doing five hundred need different numbers for the same rule. A rule
that cannot meet its minimum sample size reports ``insufficient_data`` and
does **not** fire: one bad call out of one must never page anyone.

Each firing alert carries a `filter` — the exact query string for the
Telephony dashboard — and, where the rule is about latency or failures, the
call ids that caused it. An alert you cannot click through to the evidence is
a pager that teaches people to ignore pagers.

These sit alongside `pincer/observability/alerts.py` (the golden signals)
rather than replacing it: those answer "is voice healthy", these answer "which
part of the pipeline broke".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pincer.voice.telemetry import queries, runtime
from pincer.voice.telemetry.histogram import DEFAULT_BUCKETS_MS, LatencyHistogram

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class Severity:
    PAGE = "page"
    NOTIFY = "notify"
    INFO = "info"


@dataclass(slots=True)
class TelephonyAlert:
    rule: str
    title: str
    severity: str
    firing: bool
    value: float | None
    threshold: float
    window_min: int
    samples: int
    min_samples: int
    reason: str = ""
    dashboard_filter: str = ""
    evidence: list[str] = field(default_factory=list)
    detail: str = ""

    @property
    def insufficient_data(self) -> bool:
        return self.samples < self.min_samples

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "title": self.title,
            "severity": self.severity,
            "firing": self.firing,
            "value": self.value,
            "threshold": self.threshold,
            "window_min": self.window_min,
            "samples": self.samples,
            "min_samples": self.min_samples,
            "insufficient_data": self.insufficient_data,
            "reason": self.reason,
            "dashboard_filter": self.dashboard_filter,
            "evidence": self.evidence,
            "detail": self.detail,
        }


def _cfg(settings: Any, name: str, default: Any) -> Any:
    value = getattr(settings, name, None)
    return default if value is None else value


def _window_filter(window_min: int, **extra: str) -> str:
    parts = [f"window={window_min}m", *[f"{k}={v}" for k, v in extra.items() if v]]
    return "&".join(parts)


async def evaluate(db_path: str | Path, settings: Any) -> list[TelephonyAlert]:
    """Evaluate every telephony rule against the current window."""
    alerts: list[TelephonyAlert] = []
    latency_window = int(_cfg(settings, "alert_response_latency_window_min", 30))
    rate_window = int(_cfg(settings, "alert_telephony_window_min", 60))
    min_turns = int(_cfg(settings, "telephony_min_samples", 20))
    min_calls = int(_cfg(settings, "alert_telephony_min_calls", 10))

    alerts.append(await _response_latency(db_path, settings, latency_window, min_turns))
    alerts.append(await _audio_queue(db_path, settings, latency_window, min_turns))
    alerts.extend(await _call_rates(db_path, settings, rate_window, min_calls))
    alerts.extend(await _stage_timeouts(db_path, settings, rate_window))
    alerts.append(_telemetry_export(settings))
    return alerts


async def _turn_rows(db_path: str | Path, window_min: int) -> list[Any]:
    since = (datetime.now(UTC) - timedelta(minutes=window_min)).isoformat()
    async with queries.store.service(db_path).reads() as reads:
        return [] if reads is None else await reads.turns_since(since)


async def _response_latency(db_path: str | Path, settings: Any, window_min: int, min_turns: int) -> TelephonyAlert:
    threshold = float(_cfg(settings, "alert_response_latency_p95_ms", 2000.0))
    rows = await _turn_rows(db_path, window_min)
    hist = LatencyHistogram(buckets=DEFAULT_BUCKETS_MS)
    for row in rows:
        hist.add(row["response_latency_ms"])
    p95 = hist.percentile(0.95)
    worst = sorted(
        (r for r in rows if r["response_latency_ms"] is not None),
        key=lambda r: -float(r["response_latency_ms"]),
    )[:5]
    firing = hist.count >= min_turns and p95 is not None and p95 > threshold
    return TelephonyAlert(
        rule="response_latency_p95",
        title="Response latency p95 elevated",
        severity=Severity.NOTIFY,
        firing=firing,
        value=round(p95, 1) if p95 is not None else None,
        threshold=threshold,
        window_min=window_min,
        samples=hist.count,
        min_samples=min_turns,
        reason="insufficient turns in window" if hist.count < min_turns else "",
        dashboard_filter=_window_filter(window_min, sort="response_latency_ms"),
        evidence=[str(r["provider_call_id"]) for r in worst],
        detail=(
            "Caller speech end → first response audio SENT to the provider. This is not what the "
            "caller heard; see the metric definition for the boundary."
        ),
    )


async def _audio_queue(db_path: str | Path, settings: Any, window_min: int, min_turns: int) -> TelephonyAlert:
    threshold = float(_cfg(settings, "alert_audio_queue_p95_ms", 250.0))
    rows = await _turn_rows(db_path, window_min)
    hist = LatencyHistogram(buckets=DEFAULT_BUCKETS_MS)
    for row in rows:
        hist.add(row["audio_queue_ms"])
    p95 = hist.percentile(0.95)
    firing = hist.count >= min_turns and p95 is not None and p95 > threshold
    return TelephonyAlert(
        rule="audio_queue_growth",
        title="Outbound audio queue growing",
        severity=Severity.PAGE,
        firing=firing,
        value=round(p95, 1) if p95 is not None else None,
        threshold=threshold,
        window_min=window_min,
        samples=hist.count,
        min_samples=min_turns,
        reason="insufficient turns in window" if hist.count < min_turns else "",
        dashboard_filter=_window_filter(window_min, stage="audio_queue_ms"),
        detail=(
            "Time a synthesized chunk waited inside our process before being written to the "
            "provider socket. Sustained growth means we are producing audio faster than we ship it."
        ),
    )


async def _call_rates(db_path: str | Path, settings: Any, window_min: int, min_calls: int) -> list[TelephonyAlert]:
    filters = queries.CallFilters.for_hours(window_min / 60.0)
    agg = await queries.overview(db_path, filters, min_samples=min_calls)
    rates = agg.rates
    out: list[TelephonyAlert] = []

    def rule(
        name: str,
        title: str,
        key: str,
        threshold: float,
        *,
        lower_is_bad: bool,
        severity: str,
        detail: str,
    ) -> TelephonyAlert:
        entry = rates.get(key, {})
        value = entry.get("value")
        samples = int(entry.get("denominator") or 0)
        breached = value is not None and (value < threshold if lower_is_bad else value > threshold)
        return TelephonyAlert(
            rule=name,
            title=title,
            severity=severity,
            firing=bool(samples >= min_calls and breached),
            value=round(value, 4) if value is not None else None,
            threshold=threshold,
            window_min=window_min,
            samples=samples,
            min_samples=min_calls,
            reason="insufficient calls in window" if samples < min_calls else "",
            dashboard_filter=_window_filter(window_min, failure_category="technical" if "failure" in name else ""),
            detail=detail,
        )

    out.append(
        rule(
            "connection_rate",
            "Connection rate low",
            "connection_rate",
            float(_cfg(settings, "alert_connection_rate_min", 0.90)),
            lower_is_bad=True,
            severity=Severity.PAGE,
            detail=queries.DENOMINATORS["connection_rate"],
        )
    )
    out.append(
        rule(
            "technical_failure_rate",
            "Technical failure rate high",
            "technical_failure_rate",
            float(_cfg(settings, "alert_technical_failure_rate_max", 0.05)),
            lower_is_bad=False,
            severity=Severity.PAGE,
            detail=queries.DENOMINATORS["technical_failure_rate"],
        )
    )
    out.append(
        rule(
            "unexpected_disconnect_rate",
            "Unexpected disconnects high",
            "unexpected_disconnect_rate",
            float(_cfg(settings, "alert_unexpected_disconnect_rate_max", 0.02)),
            lower_is_bad=False,
            severity=Severity.PAGE,
            detail=queries.DENOMINATORS["unexpected_disconnect_rate"],
        )
    )
    return out


async def _stage_timeouts(db_path: str | Path, settings: Any, window_min: int) -> list[TelephonyAlert]:
    """STT / LLM / TTS / tool timeouts, counted from the event stream.

    Counted rather than rated: a timeout is rare enough that N of them in an
    hour is the signal, and a rate would hide three timeouts in a busy hour.
    """
    threshold = float(_cfg(settings, "alert_stage_timeout_max", 3))
    since = (datetime.now(UTC) - timedelta(minutes=window_min)).isoformat()
    counts: dict[str, int] = {}
    evidence: dict[str, list[str]] = {}
    async with queries.store.service(db_path).reads() as reads:
        if reads is None:
            return []
        rows = await reads.events_since(since, ("timeout", "error"))

    for row in rows:
        attrs = queries.store.loads(row["attributes"], {})
        stage = str(attrs.get("stage") or "")
        if not stage:
            continue
        key = stage if row["name"] == "timeout" else f"{stage}_error"
        counts[key] = counts.get(key, 0) + 1
        evidence.setdefault(key, [])
        if len(evidence[key]) < 5:
            evidence[key].append(str(row["provider_call_id"]))

    watched = ("stt", "llm", "tts", "tool", "provider")
    out: list[TelephonyAlert] = []
    for stage in watched:
        count = counts.get(stage, 0)
        out.append(
            TelephonyAlert(
                rule=f"{stage}_timeouts",
                title=f"{stage.upper()} timeouts",
                severity=Severity.NOTIFY,
                firing=count > threshold,
                value=float(count),
                threshold=threshold,
                window_min=window_min,
                samples=count,
                min_samples=0,
                dashboard_filter=_window_filter(window_min, stage=stage),
                evidence=evidence.get(stage, []),
                detail=f"Count of `{stage}` timeout events in the window, from the call event stream.",
            )
        )

    media_failures = counts.get("provider_error", 0) + counts.get("stt_error", 0)
    out.append(
        TelephonyAlert(
            rule="media_connection_failures",
            title="Media connection failures",
            severity=Severity.PAGE,
            firing=media_failures > threshold,
            value=float(media_failures),
            threshold=threshold,
            window_min=window_min,
            samples=media_failures,
            min_samples=0,
            dashboard_filter=_window_filter(window_min),
            detail="Provider transport and STT stream errors — the media path breaking under a live call.",
        )
    )
    return out


def _telemetry_export(settings: Any) -> TelephonyAlert:
    """Telemetry that stopped arriving looks exactly like a healthy system."""
    threshold = float(_cfg(settings, "alert_telemetry_coverage_min", 0.95))
    health = runtime.health()
    export = health["export"]
    coverage = float(health["coverage"])
    dropped = int(export["dropped_queue_full"]) + int(export["export_failures"])
    return TelephonyAlert(
        rule="telemetry_export",
        title="Telemetry export degraded",
        severity=Severity.NOTIFY,
        firing=bool(health["enabled"]) and (coverage < threshold or dropped > 0),
        value=round(coverage, 4),
        threshold=threshold,
        window_min=0,
        samples=int(export["queued"]),
        min_samples=1,
        dashboard_filter="",
        detail=(
            f"{dropped} record(s) lost (queue full or export failure). "
            "Latency charts computed while this is firing are incomplete — do not read an "
            "improvement as a real one."
        ),
    )


async def firing(db_path: str | Path, settings: Any) -> list[TelephonyAlert]:
    return [a for a in await evaluate(db_path, settings) if a.firing]
