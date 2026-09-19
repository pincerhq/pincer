"""
Read side: call search, per-call detail, and the aggregate overview.

Aggregation rules that are enforced here rather than trusted to callers:

* Percentiles come off a :class:`LatencyHistogram` built from raw observations
  streamed out of SQLite. Percentiles are never averaged, and a comparison
  (provider A vs B, model X vs Y) builds a separate histogram per group.
* Every aggregate carries its own ``count``. A p99 over nine turns is reported
  with ``sufficient_samples: false`` rather than quietly rendered as fact.
* Rates carry their denominator definition (``outcomes.DENOMINATORS``).
* Coverage is reported as a first-class number: aggregate metrics computed over
  sampled telemetry are only as good as the sampling, and a dashboard that
  hides that is worse than no dashboard.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pincer.voice.telemetry import store
from pincer.voice.telemetry.histogram import DEFAULT_BUCKETS_MS, LatencyHistogram
from pincer.voice.telemetry.outcomes import DENOMINATORS, FailureCategory, is_unexpected_disconnect

if TYPE_CHECKING:
    from pathlib import Path

    import aiosqlite

logger = logging.getLogger(__name__)

#: Stage columns on `telephony_turns` that get a distribution + percentiles.
STAGE_COLUMNS: tuple[str, ...] = (
    "response_latency_ms",
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
)

#: Below this, a percentile is shown but flagged as under-sampled.
DEFAULT_MIN_SAMPLES = 20

_MAX_ROWS = 50_000


@dataclass(slots=True)
class CallFilters:
    """Everything the overview and the call table can be sliced by."""

    since: str = ""
    until: str = ""
    environment: str = ""
    app_version: str = ""
    tenant_id: str = ""
    direction: str = ""
    provider: str = ""
    engine: str = ""
    model: str = ""
    language: str = ""
    status: str = ""
    failure_category: str = ""
    failure_code: str = ""
    search: str = ""

    @classmethod
    def for_hours(cls, hours: float, **kwargs: Any) -> CallFilters:
        since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        return cls(since=since, **kwargs)

    def where(self, *, alias: str = "c") -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        simple = {
            "environment": self.environment,
            "app_version": self.app_version,
            "tenant_id": self.tenant_id,
            "direction": self.direction,
            "provider": self.provider,
            "engine": self.engine,
            "model": self.model,
            "language": self.language,
            "status": self.status,
            "failure_category": self.failure_category,
            "failure_code": self.failure_code,
        }
        for column, value in simple.items():
            if value:
                clauses.append(f"{alias}.{column} = ?")
                params.append(value)
        if self.since:
            clauses.append(f"{alias}.registered_at >= ?")
            params.append(self.since)
        if self.until:
            clauses.append(f"{alias}.registered_at <= ?")
            params.append(self.until)
        if self.search:
            needle = f"%{self.search.strip()}%"
            clauses.append(
                f"({alias}.call_id LIKE ? OR {alias}.provider_call_id LIKE ? OR {alias}.trace_id LIKE ? "
                f"OR {alias}.from_number_masked LIKE ? OR {alias}.to_number_masked LIKE ?)"
            )
            params.extend([needle] * 5)
        return (" AND ".join(clauses) or "1=1"), params


@dataclass(slots=True)
class TenantScope:
    """Which tenants the caller may see.

    ``None`` means unrestricted (single-tenant deployment or an operator with
    global access). An empty tuple means "no tenants" and every query returns
    nothing — fail closed, never fail open.
    """

    allowed: tuple[str, ...] | None = None

    def apply(self, where: str, params: list[Any], *, alias: str = "c") -> tuple[str, list[Any]]:
        if self.allowed is None:
            return where, params
        if not self.allowed:
            # The clause is replaced outright, so its placeholders go with it —
            # keeping the old params here leaves a parameter with nothing to bind.
            return "1=0", []
        placeholders = ", ".join("?" for _ in self.allowed)
        return f"({where}) AND {alias}.tenant_id IN ({placeholders})", [*params, *self.allowed]


@dataclass
class Aggregate:
    calls: dict[str, Any] = field(default_factory=dict)
    rates: dict[str, Any] = field(default_factory=dict)
    stages: dict[str, Any] = field(default_factory=dict)
    distributions: dict[str, Any] = field(default_factory=dict)
    comparisons: dict[str, Any] = field(default_factory=dict)
    reliability: dict[str, Any] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)
    trend: list[dict[str, Any]] = field(default_factory=list)
    unavailable: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "rates": self.rates,
            "stages": self.stages,
            "distributions": self.distributions,
            "comparisons": self.comparisons,
            "reliability": self.reliability,
            "coverage": self.coverage,
            "trend": self.trend,
            "unavailable": self.unavailable,
            "denominators": DENOMINATORS,
        }


async def _fetch(db: aiosqlite.Connection, sql: str, params: list[Any] | tuple[Any, ...] = ()) -> list[Any]:
    cursor = await db.execute(sql, tuple(params))
    try:
        return list(await cursor.fetchall())
    finally:
        await cursor.close()


async def search_calls(
    db_path: str | Path,
    filters: CallFilters,
    *,
    scope: TenantScope | None = None,
    limit: int = 50,
    offset: int = 0,
    sort: str = "registered_at",
    order: str = "desc",
) -> dict[str, Any]:
    """Paginated call table with a total count for the pager."""
    sortable = {
        "registered_at",
        "duration_ms",
        "turn_count",
        "setup_ms",
        "status",
        "direction",
        "engine",
        "failure_code",
    }
    sort_column = sort if sort in sortable else "registered_at"
    direction = "ASC" if str(order).lower() == "asc" else "DESC"

    where, params = filters.where()
    if scope is not None:
        where, params = scope.apply(where, params)

    async with store.connect(db_path) as db:
        if not await store.tables_present(db):
            return {"total": 0, "calls": [], "limit": limit, "offset": offset}
        total_rows = await _fetch(db, f"SELECT COUNT(*) AS n FROM telephony_calls c WHERE {where}", params)  # noqa: S608
        total = int(total_rows[0]["n"]) if total_rows else 0
        rows = await _fetch(
            db,
            f"SELECT * FROM telephony_calls c WHERE {where} "  # noqa: S608
            f"ORDER BY c.{sort_column} {direction} LIMIT ? OFFSET ?",
            [*params, int(limit), int(offset)],
        )
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "calls": [_call_row(r) for r in rows],
    }


def _call_row(row: Any) -> dict[str, Any]:
    data = {key: row[key] for key in row.keys()}  # noqa: SIM118 - aiosqlite.Row iterates values, not keys
    data["config"] = store.loads(data.pop("config_json", None), {})
    data["sampled"] = bool(data.get("sampled", 1))
    return data


async def get_call(db_path: str | Path, call_ref: str, *, scope: TenantScope | None = None) -> dict[str, Any] | None:
    """One call by internal id or by provider CallSid."""
    where = "(c.call_id = ? OR c.provider_call_id = ?)"
    params: list[Any] = [call_ref, call_ref]
    if scope is not None:
        where, params = scope.apply(where, params)
    async with store.connect(db_path) as db:
        if not await store.tables_present(db):
            return None
        rows = await _fetch(db, f"SELECT * FROM telephony_calls c WHERE {where} LIMIT 1", params)  # noqa: S608
    return _call_row(rows[0]) if rows else None


async def get_events(db_path: str | Path, call_id: str, *, limit: int = 5000) -> list[dict[str, Any]]:
    """Chronological event timeline.

    Ordered at read time by ``(ts_utc, seq)`` — arrival order is not trusted,
    which is what makes a late Twilio status callback land where it belongs
    instead of at the bottom.
    """
    async with store.connect(db_path) as db:
        if not await store.tables_present(db):
            return []
        rows = await _fetch(
            db,
            "SELECT * FROM telephony_events WHERE call_id = ? ORDER BY ts_utc ASC, seq ASC LIMIT ?",
            [call_id, int(limit)],
        )
    out: list[dict[str, Any]] = []
    for row in rows:
        data = {key: row[key] for key in row.keys()}  # noqa: SIM118 - aiosqlite.Row iterates values, not keys
        data["attributes"] = store.loads(data.pop("attributes", None), {})
        data.pop("mono_ns", None)  # process-local; meaningless to a browser
        out.append(data)
    return out


async def get_spans(db_path: str | Path, call_id: str, *, limit: int = 5000) -> list[dict[str, Any]]:
    """Every span of a call, with offsets relative to the call's first span.

    Offsets are what the waterfall renders; they are derived from monotonic
    stamps, so overlapping spans stay correctly positioned relative to each
    other even if the wall clock was stepped mid-call.
    """
    async with store.connect(db_path) as db:
        if not await store.tables_present(db):
            return []
        rows = await _fetch(
            db,
            "SELECT * FROM telephony_spans WHERE call_id = ? ORDER BY start_mono_ns ASC LIMIT ?",
            [call_id, int(limit)],
        )
    if not rows:
        return []
    origin = min(int(r["start_mono_ns"] or 0) for r in rows)
    out: list[dict[str, Any]] = []
    for row in rows:
        data = {key: row[key] for key in row.keys()}  # noqa: SIM118 - aiosqlite.Row iterates values, not keys
        data["attributes"] = store.loads(data.pop("attributes", None), {})
        start_ns = int(data.pop("start_mono_ns", 0) or 0)
        end_ns = data.pop("end_mono_ns", None)
        data["start_offset_ms"] = round((start_ns - origin) / 1_000_000.0, 2)
        data["end_offset_ms"] = None if end_ns is None else round((int(end_ns) - origin) / 1_000_000.0, 2)
        data["open"] = end_ns is None
        out.append(data)
    return out


async def get_turns(db_path: str | Path, call_id: str) -> list[dict[str, Any]]:
    async with store.connect(db_path) as db:
        if not await store.tables_present(db):
            return []
        rows = await _fetch(
            db,
            "SELECT * FROM telephony_turns WHERE call_id = ? ORDER BY turn_no ASC",
            [call_id],
        )
    out: list[dict[str, Any]] = []
    for row in rows:
        data = {key: row[key] for key in row.keys()}  # noqa: SIM118 - aiosqlite.Row iterates values, not keys
        data["critical_path"] = store.loads(data.pop("critical_path", None), [])
        data["complete"] = bool(data["complete"])
        data["interrupted"] = bool(data["interrupted"])
        data["cancelled"] = bool(data["cancelled"])
        out.append(data)
    return out


async def slowest_turns(
    db_path: str | Path,
    filters: CallFilters,
    *,
    scope: TenantScope | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Slowest turns in the window, with their bottleneck stage already attributed.

    Booleans are coerced here for the same reason ``get_turns`` and
    ``export_turns`` coerce them: SQLite stores them as 0/1, the dashboard
    declares them ``boolean`` (``TelephonyTurn`` in ``api/types.ts``, which is
    what ``client.ts`` types this endpoint's response as), and 0/1 satisfies
    that type at run time right up until the first ``=== true``.
    """
    where, params = filters.where()
    if scope is not None:
        where, params = scope.apply(where, params)
    async with store.connect(db_path) as db:
        if not await store.tables_present(db):
            return []
        rows = await _fetch(
            db,
            "SELECT t.*, c.provider_call_id, c.direction FROM telephony_turns t "
            f"JOIN telephony_calls c ON c.call_id = t.call_id WHERE {where} "  # noqa: S608
            "AND t.response_latency_ms IS NOT NULL ORDER BY t.response_latency_ms DESC LIMIT ?",
            [*params, int(limit)],
        )
    out: list[dict[str, Any]] = []
    for row in rows:
        data = {key: row[key] for key in row.keys()}  # noqa: SIM118 - aiosqlite.Row iterates values, not keys
        data["critical_path"] = store.loads(data.pop("critical_path", None), [])
        data["complete"] = bool(data["complete"])
        data["interrupted"] = bool(data["interrupted"])
        data["cancelled"] = bool(data["cancelled"])
        out.append(data)
    return out


async def export_turns(
    db_path: str | Path,
    filters: CallFilters,
    *,
    scope: TenantScope | None = None,
    limit: int = 50_000,
) -> list[dict[str, Any]]:
    """Every turn in the window, joined to its call's provider id.

    For the download path: flat rows, no nested critical path (a JSON blob in a
    CSV cell helps nobody — the per-call API serves the path when it is wanted).
    """
    where, params = filters.where()
    if scope is not None:
        where, params = scope.apply(where, params)
    async with store.connect(db_path) as db:
        if not await store.tables_present(db):
            return []
        rows = await _fetch(
            db,
            "SELECT t.*, c.provider_call_id AS provider_call_id FROM telephony_turns t "
            f"JOIN telephony_calls c ON c.call_id = t.call_id WHERE {where} "  # noqa: S608
            "ORDER BY t.created_at DESC LIMIT ?",
            [*params, int(limit)],
        )
    out: list[dict[str, Any]] = []
    for row in rows:
        data = {key: row[key] for key in row.keys()}  # noqa: SIM118 - aiosqlite.Row iterates values, not keys
        data.pop("critical_path", None)
        data["interrupted"] = bool(data["interrupted"])
        data["cancelled"] = bool(data["cancelled"])
        data["complete"] = bool(data["complete"])
        out.append(data)
    return out


async def overview(
    db_path: str | Path,
    filters: CallFilters,
    *,
    scope: TenantScope | None = None,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    trend_buckets: int = 24,
) -> Aggregate:
    """The telephony overview page, computed in one pass over the window."""
    where, params = filters.where()
    if scope is not None:
        where, params = scope.apply(where, params)

    agg = Aggregate()
    async with store.connect(db_path) as db:
        if not await store.tables_present(db):
            agg.coverage = {
                "telemetry_tables": False,
                "note": "Telephony telemetry tables are not present — run `pincer db upgrade`.",
            }
            return agg

        call_rows = await _fetch(db, f"SELECT * FROM telephony_calls c WHERE {where} LIMIT ?", [*params, _MAX_ROWS])  # noqa: S608
        turn_rows = await _fetch(
            db,
            "SELECT t.*, c.engine AS call_engine, c.model AS call_model, c.provider AS call_provider, "
            "c.direction AS call_direction, c.registered_at AS call_registered_at "
            f"FROM telephony_turns t JOIN telephony_calls c ON c.call_id = t.call_id WHERE {where} LIMIT ?",  # noqa: S608
            [*params, _MAX_ROWS],
        )
        # Transport-level signals live only as events (they have no per-turn
        # grain), so they are counted separately rather than inferred.
        event_rows = await _fetch(
            db,
            "SELECT e.name AS name, COUNT(*) AS n FROM telephony_events e "
            f"JOIN telephony_calls c ON c.call_id = e.call_id WHERE {where} "  # noqa: S608
            "AND e.name IN ('audio.gap', 'bargein.detected', 'audio.buffer_cleared', 'reconnect') "
            "GROUP BY e.name",
            params,
        )

    event_counts = {str(row["name"]): int(row["n"]) for row in event_rows}
    agg.calls = _call_counts(call_rows)
    agg.rates = _rates(call_rows)
    agg.reliability = _reliability(call_rows, turn_rows, event_counts)
    agg.stages = _stage_summaries(turn_rows, min_samples=min_samples)
    agg.distributions = _distributions(turn_rows)
    agg.comparisons = _comparisons(turn_rows, min_samples=min_samples)
    agg.coverage = _coverage(call_rows, turn_rows)
    agg.trend = _trend(turn_rows, buckets=trend_buckets)
    agg.unavailable = _unavailable(call_rows)
    return agg


def _call_counts(rows: list[Any]) -> dict[str, Any]:
    attempted = 0
    declined = 0
    connected = 0
    completed = 0
    failed = 0
    active = 0
    durations = LatencyHistogram(buckets=(5e3, 15e3, 30e3, 60e3, 120e3, 180e3, 300e3, 600e3, 900e3))
    turns = LatencyHistogram(buckets=(1, 2, 3, 5, 8, 12, 20, 40))
    for row in rows:
        category = str(row["failure_category"] or "")
        status = str(row["status"] or "")
        if status == "active":
            active += 1
        if category == FailureCategory.POLICY_DECLINED:
            declined += 1
        else:
            attempted += 1
        if row["answered_at"]:
            connected += 1
        if status == "completed":
            completed += 1
        elif status in ("failed", "ended") and category == FailureCategory.TECHNICAL:
            failed += 1
        if row["duration_ms"] is not None:
            durations.add(float(row["duration_ms"]))
        turns.add(float(row["turn_count"] or 0))
    return {
        "total": len(rows),
        "attempted": attempted,
        "declined_by_policy": declined,
        "connected": connected,
        "completed": completed,
        "technical_failures": failed,
        "active": active,
        "duration_distribution": durations.distribution(),
        "duration_summary": durations.summary(min_samples=1),
        "turn_count_distribution": turns.distribution(),
        "turn_count_summary": turns.summary(min_samples=1),
    }


def _rates(rows: list[Any]) -> dict[str, Any]:
    attempted = [r for r in rows if str(r["failure_category"] or "") != FailureCategory.POLICY_DECLINED]
    terminated = [r for r in rows if str(r["status"] or "") not in ("active", "")]
    connected = [r for r in attempted if r["answered_at"]]
    technical = [r for r in terminated if str(r["failure_category"] or "") == FailureCategory.TECHNICAL]
    disconnects = [r for r in connected if is_unexpected_disconnect(r["failure_code"])]
    return {
        "connection_rate": _rate(len(connected), len(attempted)),
        "technical_failure_rate": _rate(len(technical), len(terminated)),
        "unexpected_disconnect_rate": _rate(len(disconnects), len(connected)),
    }


def _rate(numerator: int, denominator: int) -> dict[str, Any]:
    """A rate always ships with both of its parts.

    ``value`` is ``None`` rather than 0.0 on an empty denominator — "no calls"
    is not "0% success", and a gauge that shows 0% for an idle hour trains
    people to ignore it.
    """
    return {
        "value": (numerator / denominator) if denominator else None,
        "numerator": numerator,
        "denominator": denominator,
    }


def _reliability(call_rows: list[Any], turn_rows: list[Any], event_counts: dict[str, int]) -> dict[str, Any]:
    return {
        # Audio quality, to the extent this transport exposes any: gaps in
        # inbound frame ARRIVAL. There is no RTT, jitter or packet-loss figure
        # to report — Twilio terminates the RTP leg (see METRICS).
        "audio_gaps": event_counts.get("audio.gap", 0),
        "barge_ins": event_counts.get("bargein.detected", 0),
        "buffer_clears": event_counts.get("audio.buffer_cleared", 0),
        "errors": sum(int(r["error_count"] or 0) for r in call_rows),
        "timeouts": sum(int(r["timeout_count"] or 0) for r in call_rows),
        "retries": sum(int(r["retry_count"] or 0) for r in call_rows),
        "interruptions": sum(int(r["interruption_count"] or 0) for r in call_rows),
        "reconnects": sum(int(r["reconnect_count"] or 0) for r in call_rows),
        "tool_calls": sum(int(r["tool_count"] or 0) for r in call_rows),
        "tool_timeouts": sum(int(r["tool_timeouts"] or 0) for r in turn_rows),
        "tool_retries": sum(int(r["tool_retries"] or 0) for r in turn_rows),
        "cancelled_turns": sum(1 for r in turn_rows if r["cancelled"]),
    }


def _stage_summaries(turn_rows: list[Any], *, min_samples: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for column in STAGE_COLUMNS:
        hist = LatencyHistogram(buckets=DEFAULT_BUCKETS_MS)
        for row in turn_rows:
            hist.add(row[column])
        out[column] = hist.summary(min_samples=min_samples)
    return out


def _distributions(turn_rows: list[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for column in STAGE_COLUMNS:
        hist = LatencyHistogram(buckets=DEFAULT_BUCKETS_MS)
        for row in turn_rows:
            hist.add(row[column])
        if hist.count:
            out[column] = hist.distribution()
    return out


def _comparisons(turn_rows: list[Any], *, min_samples: int) -> dict[str, Any]:
    """Provider / model / engine comparison of response latency.

    One histogram per group; percentiles are read off each group's own
    histogram. Nothing here averages another percentile.
    """
    groups: dict[str, dict[str, LatencyHistogram]] = {"engine": {}, "model": {}, "provider": {}, "direction": {}}
    key_columns = {
        "engine": "call_engine",
        "model": "call_model",
        "provider": "call_provider",
        "direction": "call_direction",
    }
    for row in turn_rows:
        for group, column in key_columns.items():
            key = str(row[column] or "") or "unknown"
            hist = groups[group].setdefault(key, LatencyHistogram(buckets=DEFAULT_BUCKETS_MS))
            hist.add(row["response_latency_ms"])
    return {
        group: [
            {"key": key, **hist.summary(min_samples=min_samples)}
            for key, hist in sorted(members.items(), key=lambda kv: -kv[1].count)
        ]
        for group, members in groups.items()
    }


def _coverage(call_rows: list[Any], turn_rows: list[Any]) -> dict[str, Any]:
    sampled = sum(1 for r in call_rows if int(r["sampled"] or 0))
    partial = sum(1 for r in call_rows if str(r["coverage"] or "full") != "full")
    incomplete_turns = sum(1 for r in turn_rows if not int(r["complete"] or 0))
    turns_without_latency = sum(1 for r in turn_rows if r["response_latency_ms"] is None)
    rates = {float(r["sample_rate_used"] or 1.0) for r in call_rows} or {1.0}
    return {
        "calls": len(call_rows),
        "calls_with_telemetry": sampled,
        "calls_partial": partial,
        "turns": len(turn_rows),
        "turns_incomplete": incomplete_turns,
        "turns_without_response_latency": turns_without_latency,
        "sample_rates_seen": sorted(rates),
        "note": (
            "Aggregates are computed over sampled calls only. `calls_partial` are calls whose "
            "telemetry started or stopped mid-call (upgrade on failure, exporter drop, restart); "
            "their turn-level rows are usable but not a complete picture of the call."
        ),
    }


def _trend(turn_rows: list[Any], *, buckets: int) -> list[dict[str, Any]]:
    """Response-latency percentiles over time.

    Each bucket gets its own histogram built from that bucket's raw turns —
    never a rolled-up percentile of percentiles.
    """
    stamped = [(str(r["created_at"] or ""), r) for r in turn_rows if r["created_at"]]
    if not stamped:
        return []
    stamped.sort(key=lambda kv: kv[0])
    first, last = stamped[0][0], stamped[-1][0]
    try:
        start = datetime.fromisoformat(first)
        end = datetime.fromisoformat(last)
    except ValueError:
        return []
    span = max((end - start).total_seconds(), 1.0)
    width = span / max(1, buckets)

    slots: list[LatencyHistogram] = [LatencyHistogram(buckets=DEFAULT_BUCKETS_MS) for _ in range(buckets)]
    counts = [0] * buckets
    for stamp, row in stamped:
        try:
            offset = (datetime.fromisoformat(stamp) - start).total_seconds()
        except ValueError:
            continue
        index = min(buckets - 1, int(offset / width))
        slots[index].add(row["response_latency_ms"])
        counts[index] += 1

    out: list[dict[str, Any]] = []
    for index, hist in enumerate(slots):
        if not counts[index]:
            continue
        out.append(
            {
                "bucket_start": (start + timedelta(seconds=index * width)).isoformat(),
                "turns": counts[index],
                **hist.summary(min_samples=1),
            }
        )
    return out


def _unavailable(call_rows: list[Any]) -> list[dict[str, Any]]:
    """Metrics that cannot be produced for the engines present in this window.

    Shown on the overview so a missing chart reads as "structurally impossible
    here" rather than "broken".
    """
    from pincer.voice.telemetry.schema import METRICS

    engines = {str(r["engine"] or "") for r in call_rows if r["engine"]}
    out: list[dict[str, Any]] = []
    for metric in METRICS.values():
        blocked_on = sorted(e for e in engines if not metric.available(e))
        if not metric.available_on:
            out.append({**metric.to_dict(), "engines": sorted(engines)})
        elif blocked_on:
            out.append({**metric.to_dict(), "engines": blocked_on})
    return out
