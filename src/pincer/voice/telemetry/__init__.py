"""
Telephony telemetry: correlated events, spans and latency metrics for voice calls.

Entry points
------------
``runtime.configure(settings)``     start the exporter (called from the CLI/API startup)
``runtime.start_call(...)``         register a call, get its :class:`CallTracer`
``runtime.tracer_for(call_sid)``    the tracer a webhook or WebSocket should use
``hooks``                           thin, exception-proof shims the voice code calls

Read the design in ``docs/telephony-pipeline.md`` (what the pipeline actually
does) and ``docs/telephony-telemetry.md`` (schema, metric definitions, and how
to diagnose a slow or failed call).
"""

from __future__ import annotations

from pincer.voice.telemetry.context import CallContext, TurnContext, bind_call, bind_turn, current_call, current_turn
from pincer.voice.telemetry.recorder import ExportStats, TelemetryRecorder, get_recorder
from pincer.voice.telemetry.schema import METRICS, EventName, MeasurementSource, SpanName, SpanStatus
from pincer.voice.telemetry.tracer import CallTracer, TurnTracer

__all__ = [
    "METRICS",
    "CallContext",
    "CallTracer",
    "EventName",
    "ExportStats",
    "MeasurementSource",
    "SpanName",
    "SpanStatus",
    "TelemetryRecorder",
    "TurnContext",
    "TurnTracer",
    "bind_call",
    "bind_turn",
    "current_call",
    "current_turn",
    "get_recorder",
]
