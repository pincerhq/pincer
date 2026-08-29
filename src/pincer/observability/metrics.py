"""
OpenTelemetry metric emission for the golden signals (Sprint 9, T9.1).

`pincer_telemetry.init()` (called from `cli.run` when `PINCER_TELEMETRY_DSN` is
set) configures the global OTel providers; this module only *uses* them. When
the `telemetry` extra is not installed, every function here is a no-op — voice
calls must never fail because a metrics backend is missing, and the golden
signals are computed from SQLite anyway (`golden_signals.py`), so alerting keeps
working on a bare install.

Instrument names use the `pincer.voice.*` namespace so they group in Uptrace /
Grafana. Every instrument carries the same four dimensions where they apply —
`language`, `engine`, `direction`, `outcome` — because every operational
question so far has been "…but only for German outbound calls on
ConversationRelay".

Cardinality: labels are bounded enums (a language code, two engines, two
directions, the `FailureCode` taxonomy). `call_sid` is deliberately NOT a label
— per-call detail lives in the `call_costs` table, not in the metric store.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

METER_NAME = "pincer.voice"

# Explicit histogram buckets. The defaults (0, 5, 10, 25, …) are useless for
# sub-second latencies — p95 would round to a single bucket and the 2.0s SLO
# boundary must be a bucket edge for the SLI to be readable off the histogram.
TURN_LATENCY_BUCKETS = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 6.0, 10.0]
STAGE_LATENCY_BUCKETS = [25.0, 50.0, 100.0, 200.0, 400.0, 800.0, 1200.0, 2000.0, 3000.0, 5000.0]
CALL_DURATION_BUCKETS = [5.0, 15.0, 30.0, 60.0, 120.0, 180.0, 300.0, 600.0, 900.0]
CALL_COST_BUCKETS = [0.01, 0.025, 0.05, 0.1, 0.2, 0.35, 0.5, 1.0, 2.0, 5.0]
# Talk ratio is a proportion; even tenths make the "agent dominates" tail readable.
TALK_RATIO_BUCKETS = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]

_instruments: dict[str, Any] | None = None
_unavailable = False
_active_calls_provider: Callable[[], int] | None = None
_stuck_calls_provider: Callable[[], int] | None = None


def _build_instruments() -> dict[str, Any] | None:
    """Create the instrument set once, or return None when OTel is absent."""
    global _unavailable  # noqa: PLW0603
    if _unavailable:
        return None
    try:
        from opentelemetry import metrics as otel_metrics
    except ImportError:
        _unavailable = True
        logger.debug("OpenTelemetry not installed — voice metrics are a no-op")
        return None

    try:
        meter = otel_metrics.get_meter(METER_NAME)
        built: dict[str, Any] = {
            "calls_started": meter.create_counter(
                "pincer.voice.calls.started",
                unit="1",
                description="Calls started, by direction/engine/language",
            ),
            "calls_ended": meter.create_counter(
                "pincer.voice.calls.ended",
                unit="1",
                description="Calls terminated, by outcome and failure_code "
                "— the call success rate numerator and denominator",
            ),
            "bookings": meter.create_counter(
                "pincer.voice.bookings",
                unit="1",
                description="Appointment call outcomes, by result (confirmed/declined/unreachable)",
            ),
            "call_duration": meter.create_histogram(
                "pincer.voice.call.duration",
                unit="s",
                description="Call wall-clock duration",
                explicit_bucket_boundaries_advisory=CALL_DURATION_BUCKETS,
            ),
            "turn_latency": meter.create_histogram(
                "pincer.voice.turn.latency",
                unit="s",
                description="Voice-to-voice turn latency (caller stopped speaking -> agent audio dispatched)",
                explicit_bucket_boundaries_advisory=TURN_LATENCY_BUCKETS,
            ),
            "turn_stage": meter.create_histogram(
                "pincer.voice.turn.stage",
                unit="ms",
                description="Per-stage turn latency (prep, llm_first_token, first_sentence, first_dispatch, llm_done)",
                explicit_bucket_boundaries_advisory=STAGE_LATENCY_BUCKETS,
            ),
            "call_cost": meter.create_histogram(
                "pincer.voice.call.cost",
                unit="USD",
                description="Total cost of one call (Twilio + STT + TTS + LLM)",
                explicit_bucket_boundaries_advisory=CALL_COST_BUCKETS,
            ),
            "cost_component": meter.create_counter(
                "pincer.voice.cost.component",
                unit="USD",
                description="Cost accrued per component (twilio/stt/tts/llm)",
            ),
            "canary_runs": meter.create_counter(
                "pincer.voice.canary.runs",
                unit="1",
                description="Synthetic canary call results (ok/failed)",
            ),
            "alerts_fired": meter.create_counter(
                "pincer.voice.alerts.fired",
                unit="1",
                description="Alert rule firings, by rule and severity",
            ),
            # Sprint 12: inbound receptionist events (answered, intent, booking, transfer, …)
            "inbound_events": meter.create_counter(
                "pincer.voice.inbound.events",
                unit="1",
                description="Receptionist events: answered, intent (by intent), message_taken, booking, "
                "transfer, silent_hangup, busy_capacity, blocked",
            ),
            # Sprint 11: every in-call tool decision, by tier / action / reason / mode
            "tool_decisions": meter.create_counter(
                "pincer.voice.tool.decisions",
                unit="1",
                description="In-call tool policy decisions (execute/deny/need_verbal/need_user_approval), "
                "with the stable deny reason code as a label",
            ),
            # Conversation analytics: who spoke how much, and how the caller
            # seemed about it. `method` is a label on the ratio histogram so an
            # estimated distribution is never silently pooled with measured one.
            "talk_ratio": meter.create_histogram(
                "pincer.voice.talk_ratio",
                unit="1",
                description="Agent share of speaking time per call, 0-1 (label: method=exact|estimated)",
                explicit_bucket_boundaries_advisory=TALK_RATIO_BUCKETS,
            ),
            "sentiment": meter.create_counter(
                "pincer.voice.sentiment",
                unit="1",
                description="Caller sentiment per finished call (positive/neutral/negative/mixed)",
            ),
            "interruptions": meter.create_counter(
                "pincer.voice.interruptions",
                unit="1",
                description="Barge-in events: the caller started while agent audio was playing",
            ),
            # Call briefing: did the agent's opening turns reference its task?
            # The smoke detector for "the purpose stopped reaching the agent".
            "briefing_adherence": meter.create_counter(
                "pincer.voice.briefing.adherence",
                unit="1",
                description="Outbound calls whose opening turns did (adhered=true) or did not "
                "(adhered=false) reference the task the user gave",
            ),
            # Sprint 15: live listen-in. Frames dropped by the fan-out hub
            # because a listener was too slow — audio skips, memory never grows.
            "listen_frames_dropped": meter.create_counter(
                "pincer.voice.listen.frames_dropped",
                unit="1",
                description="Listen-in media frames dropped on a slow dashboard listener (drop-oldest)",
            ),
            "listen_sessions": meter.create_counter(
                "pincer.voice.listen.sessions",
                unit="1",
                description="Listen-in sessions ended, by end reason (call_ended/stopped/capacity/error)",
            ),
        }
        meter.create_observable_gauge(
            "pincer.voice.calls.active",
            callbacks=[_observe_active_calls],
            unit="1",
            description="Calls currently in progress",
        )
        meter.create_observable_gauge(
            "pincer.voice.calls.stuck",
            callbacks=[_observe_stuck_calls],
            unit="1",
            description="Calls active past max_call_duration + grace — pages on any non-zero value",
        )
        return built
    except Exception:
        # A backend misconfiguration must not take voice down with it.
        _unavailable = True
        logger.warning("Voice metric instruments could not be created — metrics disabled", exc_info=True)
        return None


def _get() -> dict[str, Any] | None:
    global _instruments  # noqa: PLW0603
    if _instruments is None:
        _instruments = _build_instruments()
    return _instruments


def metrics_enabled() -> bool:
    """True when instruments exist (OTel installed and instrument creation worked)."""
    return _get() is not None


def reset_for_tests() -> None:
    """Drop the cached instruments so a test can re-create them."""
    global _instruments, _unavailable  # noqa: PLW0603
    _instruments = None
    _unavailable = False


# ── Active-call gauge ────────────────────────────────────────────────


def set_active_calls_provider(provider: Callable[[], int] | None) -> None:
    """Register the callable the active-call gauge reads (the voice engine)."""
    global _active_calls_provider  # noqa: PLW0603
    _active_calls_provider = provider


def set_stuck_calls_provider(provider: Callable[[], int] | None) -> None:
    """Register the callable the stuck-call gauge reads.

    A gauge rather than a counter: "how many are stuck right now" is the
    question that pages, and a counter would keep alerting long after the
    reaper cleaned them up.
    """
    global _stuck_calls_provider  # noqa: PLW0603
    _stuck_calls_provider = provider


def _observe(provider: Callable[[], int] | None, label: str) -> Any:
    from opentelemetry.metrics import Observation

    if provider is None:
        return [Observation(0)]
    try:
        return [Observation(int(provider()))]
    except Exception:  # pragma: no cover — a broken provider must not break scraping
        logger.debug("%s gauge provider failed", label, exc_info=True)
        return [Observation(0)]


def _observe_active_calls(options: Any) -> Any:
    return _observe(_active_calls_provider, "Active-call")


def _observe_stuck_calls(options: Any) -> Any:
    return _observe(_stuck_calls_provider, "Stuck-call")


# ── Emission helpers ─────────────────────────────────────────────────
#
# Each one swallows its own errors: a metrics backend hiccup must never
# propagate into a live call.


def _dims(**kwargs: Any) -> dict[str, str]:
    """Normalise label values; drop empties so a missing dimension doesn't
    become the literal string 'None' in the metric store."""
    return {k: str(v) for k, v in kwargs.items() if v not in (None, "")}


def _safe(action: Callable[[], None]) -> None:
    try:
        action()
    except Exception:  # pragma: no cover
        logger.debug("Voice metric emission failed", exc_info=True)


def _record(instrument: Any, value: float, dims: dict[str, str]) -> None:
    """Histogram record that never raises (named, not a lambda, so the
    late-binding trap in a loop cannot reappear)."""
    _safe(lambda: instrument.record(value, dims))


def _add(instrument: Any, value: float, dims: dict[str, str]) -> None:
    """Counter add that never raises."""
    _safe(lambda: instrument.add(value, dims))


def record_call_started(*, direction: str, engine: str = "", language: str = "") -> None:
    inst = _get()
    if inst is None:
        return
    _safe(lambda: inst["calls_started"].add(1, _dims(direction=direction, engine=engine, language=language)))


def record_call_ended(
    *,
    direction: str,
    outcome: str,
    failure_code: str = "none",
    engine: str = "",
    language: str = "",
    duration_s: float | None = None,
) -> None:
    """The call success rate is derived from this counter: `outcome` is
    `completed` or `failed`, and `failure_code` explains the failures."""
    inst = _get()
    if inst is None:
        return
    dims = _dims(direction=direction, outcome=outcome, failure_code=failure_code, engine=engine, language=language)
    _safe(lambda: inst["calls_ended"].add(1, dims))
    if duration_s is not None:
        _safe(lambda: inst["call_duration"].record(float(duration_s), dims))


def record_inbound_event(event: str, *, intent: str = "", language: str = "") -> None:
    """Sprint 12 §13: inbound_answered_total, inbound_intent{intent=},
    inbound_messages_taken, inbound_booking (conversion = booking/appointment
    intents), inbound_transfer, inbound_silent_hangup, busy_capacity_total, blocked."""
    inst = _get()
    if inst is None:
        return
    _safe(lambda: inst["inbound_events"].add(1, _dims(event=event, intent=intent, language=language)))


def record_tool_decision(*, tool: str, action: str, reason: str = "", tier: str = "", mode: str = "") -> None:
    """Sprint 11 §5.2: deny reasons are metric labels, so "why were writes
    refused this week" is a query rather than a log grep."""
    inst = _get()
    if inst is None:
        return
    _safe(lambda: inst["tool_decisions"].add(1, _dims(tool=tool, action=action, reason=reason, tier=tier, mode=mode)))


def record_call_analytics(
    *,
    talk_ratio: float | None,
    sentiment: str = "",
    interruptions: int = 0,
    method: str = "",
    engine: str = "",
    direction: str = "",
    language: str = "",
) -> None:
    """One call's conversation analytics. Absent values are simply not recorded
    — a call with no measurable speech must not land in the ratio histogram as
    a zero, which would read as "the agent said nothing"."""
    inst = _get()
    if inst is None:
        return
    dims = _dims(engine=engine, direction=direction, language=language)
    if talk_ratio is not None:
        _record(inst["talk_ratio"], float(talk_ratio), {**dims, "method": method})
    if sentiment:
        _add(inst["sentiment"], 1, {**dims, "sentiment": sentiment})
    if interruptions > 0:
        _add(inst["interruptions"], int(interruptions), dims)


def record_briefing_adherence(*, adhered: bool, language: str = "") -> None:
    """One datapoint per finished outbound call. A rising `adhered=false` rate
    means briefings stopped reaching the agent — the regression this whole
    mechanism exists to make loud."""
    inst = _get()
    if inst is None:
        return
    _safe(lambda: inst["briefing_adherence"].add(1, _dims(adhered=str(adhered).lower(), language=language)))


def record_booking(*, result: str, language: str = "", attempts: int = 1) -> None:
    """Appointment outcome — `confirmed`, `declined`, `unreachable`, `failed`."""
    inst = _get()
    if inst is None:
        return
    _safe(lambda: inst["bookings"].add(1, _dims(result=result, language=language, attempts=attempts)))


def record_turn_latency(
    *,
    total_s: float,
    engine: str = "",
    language: str = "",
    streamed: bool = False,
    error: bool = False,
    stages_ms: dict[str, float] | None = None,
) -> None:
    """T9.1: the Sprint 5 stage timings become histograms, not just log lines."""
    inst = _get()
    if inst is None:
        return
    dims = _dims(engine=engine, language=language, streamed=streamed, error=error)
    _safe(lambda: inst["turn_latency"].record(float(total_s), dims))
    for stage, value in (stages_ms or {}).items():
        if not isinstance(value, int | float):
            continue
        stage_dims = {**dims, "stage": stage.removesuffix("_ms")}
        _record(inst["turn_stage"], float(value), stage_dims)


def record_call_cost(
    *,
    total_usd: float,
    components: dict[str, float] | None = None,
    direction: str = "",
    engine: str = "",
    language: str = "",
) -> None:
    inst = _get()
    if inst is None:
        return
    dims = _dims(direction=direction, engine=engine, language=language)
    _safe(lambda: inst["call_cost"].record(float(total_usd), dims))
    for component, amount in (components or {}).items():
        if not amount:
            continue
        _add(inst["cost_component"], float(amount), {**dims, "component": component})


def record_canary_run(*, ok: bool, reason: str = "", duration_s: float | None = None) -> None:
    inst = _get()
    if inst is None:
        return
    _safe(lambda: inst["canary_runs"].add(1, _dims(result="ok" if ok else "failed", reason=reason if not ok else "")))
    if duration_s is not None and ok:
        _safe(lambda: inst["call_duration"].record(float(duration_s), _dims(direction="canary")))


def record_listen_frames_dropped(count: int = 1, *, track: str = "") -> None:
    """Sprint 15: a slow listener lost `count` frames (drop-oldest backpressure)."""
    inst = _get()
    if inst is None or count <= 0:
        return
    _add(inst["listen_frames_dropped"], float(count), _dims(track=track))


def record_listen_session(*, reason: str, duration_s: float | None = None) -> None:
    """Sprint 15: one listen-in session ended (`reason`: call_ended/stopped/capacity/error)."""
    inst = _get()
    if inst is None:
        return
    _add(inst["listen_sessions"], 1.0, _dims(reason=reason))


def record_alert_fired(*, rule: str, severity: str) -> None:
    inst = _get()
    if inst is None:
        return
    _safe(lambda: inst["alerts_fired"].add(1, _dims(rule=rule, severity=severity)))
