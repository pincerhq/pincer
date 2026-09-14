"""Observability: metrics emission, golden signals, alerting, canary, digests (Sprint 9).

The split mirrors what an operator actually needs:

``metrics``         — OTel counters/histograms/gauges emitted as things happen.
``failure_codes``   — the stable taxonomy every failure path is tagged with.
``golden_signals``  — the five numbers that define "healthy", computed from SQLite
                      so they work with or without a telemetry backend.
``alerts``          — rules over the golden signals, split page vs notify.
``canary``          — the synthetic loop call that catches provider breakage.
``digest``          — the weekly failure summary to the ops channel.

Metrics emission degrades to a no-op when OpenTelemetry is not installed
(`telemetry` is an optional extra) — the golden signals are computed from the
database either way, so alerting works on a bare install.
"""

from __future__ import annotations

from pincer.observability.failure_codes import FailureCode, classify_failure
from pincer.observability.metrics import (
    metrics_enabled,
    record_call_cost,
    record_call_ended,
    record_call_started,
    record_canary_run,
    record_turn_latency,
    set_active_calls_provider,
)

__all__ = [
    "FailureCode",
    "classify_failure",
    "metrics_enabled",
    "record_call_cost",
    "record_call_ended",
    "record_call_started",
    "record_canary_run",
    "record_turn_latency",
    "set_active_calls_provider",
]
