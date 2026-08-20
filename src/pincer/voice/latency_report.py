"""
Latency report over TURN_LATENCY records (Sprint 5, T5.1).

The voice channel appends one JSON line per turn to
``data/logs/voice_latency.jsonl``; this module aggregates the last N calls
into p50/p95 per stage for the ``pincer voice latency-report`` CLI and the
sprint's measurement protocol (baseline → per-PR before/after → final run).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

# Stage keys in display order; anything else numeric is appended after.
STAGE_ORDER = (
    "prep_ms",
    "llm_first_token_ms",
    "first_sentence_ms",
    "first_dispatch_ms",
    "llm_done_ms",
    "total_ms",
)


def read_turn_records(path: Path, last_calls: int = 20) -> list[dict[str, Any]]:
    """Parse the JSONL file, keeping only turns from the most recent N calls."""
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("call_sid"):
            records.append(record)

    # Most recent N distinct calls, preserving turn order within them
    recent_calls: list[str] = []
    for record in reversed(records):
        sid = str(record["call_sid"])
        if sid not in recent_calls:
            recent_calls.append(sid)
        if len(recent_calls) >= last_calls:
            break
    keep = set(recent_calls)
    return [r for r in records if str(r["call_sid"]) in keep]


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = (len(ordered) - 1) * pct
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def build_latency_report(records: list[dict[str, Any]]) -> dict[str, Any]:
    """p50/p95 per stage over the given turn records."""
    stages: dict[str, list[float]] = {}
    calls: set[str] = set()
    engines: set[str] = set()
    for record in records:
        calls.add(str(record.get("call_sid", "")))
        if record.get("engine"):
            engines.add(str(record["engine"]))
        for key, value in record.items():
            if key.endswith("_ms") and isinstance(value, int | float):
                stages.setdefault(key, []).append(float(value))

    ordered_keys = [k for k in STAGE_ORDER if k in stages] + sorted(k for k in stages if k not in STAGE_ORDER)
    return {
        "turns": len(records),
        "calls": len(calls),
        "engines": sorted(engines),
        "stages": {
            key: {
                "p50": round(_percentile(stages[key], 0.50), 1),
                "p95": round(_percentile(stages[key], 0.95), 1),
                "n": len(stages[key]),
            }
            for key in ordered_keys
        },
    }


# Stages shown per model in ``pincer voice latency-model`` (display order).
MODEL_STAGES = ("total_ms", "llm_first_token_ms", "first_dispatch_ms", "llm_done_ms")

UNKNOWN_MODEL = "unknown"


def build_model_report(records: list[dict[str, Any]], *, sort: str = "p50") -> list[dict[str, Any]]:
    """Latency per LLM model (the ``turn_model`` stamped on each turn record).

    Records that predate the ``turn_model`` stamp — or turns that failed before
    a model answered — are grouped under ``"unknown"``.

    ``sort`` is ``"p50"`` (fastest ``total_ms`` p50 first; models without a
    ``total_ms`` sort last) or ``"name"`` (alphabetical by model id).
    """
    if sort not in ("p50", "name"):
        raise ValueError(f"sort must be 'p50' or 'name', got {sort!r}")

    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        model = str(record.get("turn_model") or UNKNOWN_MODEL)
        grouped.setdefault(model, []).append(record)

    rows: list[dict[str, Any]] = []
    for model, turns in grouped.items():
        report = build_latency_report(turns)
        stages = {key: report["stages"][key] for key in MODEL_STAGES if key in report["stages"]}
        errors = sum(1 for r in turns if r.get("error"))
        rows.append(
            {
                "model": model,
                "turns": report["turns"],
                "calls": report["calls"],
                "errors": errors,
                "engines": report["engines"],
                "stages": stages,
            }
        )

    if sort == "name":
        rows.sort(key=lambda row: row["model"])
    else:
        rows.sort(
            key=lambda row: (
                "total_ms" not in row["stages"],
                row["stages"].get("total_ms", {}).get("p50", float("inf")),
                row["model"],
            )
        )
    return rows
