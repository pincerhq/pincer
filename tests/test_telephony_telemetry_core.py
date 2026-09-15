"""
Latency arithmetic, critical-path attribution, and the content policy.

Everything here uses **deterministic timing fixtures** — monotonic nanosecond
values passed in explicitly — because a latency test that sleeps measures the
machine it runs on, not the code. The numbers below are exact by construction,
so an off-by-one in an attribution rule fails the test rather than hiding in
tolerance.
"""

from __future__ import annotations

import pytest

from pincer.voice.telemetry import context as ctxmod
from pincer.voice.telemetry.clock import Stopwatch, cross_process_gap, duration_ms, mono_ns, project_utc
from pincer.voice.telemetry.critical_path import UNATTRIBUTED, PathSpan, compute
from pincer.voice.telemetry.histogram import LatencyHistogram, build
from pincer.voice.telemetry.outcomes import FailureCategory, categorise, is_unexpected_disconnect
from pincer.voice.telemetry.records import deterministic_event_id, safe_attributes
from pincer.voice.telemetry.schema import METRICS, MeasurementSource

MS = 1_000_000  # nanoseconds


@pytest.fixture(autouse=True)
def _clean_context():
    ctxmod.reset_for_tests()
    yield
    ctxmod.reset_for_tests()


# ── clocks ───────────────────────────────────────────────────────────


def test_durations_come_only_from_monotonic_pairs():
    assert duration_ms(0, 1_500 * MS) == 1500.0
    assert duration_ms(None, 5) is None
    assert duration_ms(5, None) is None


def test_cross_process_gap_refuses_to_produce_a_number():
    """Subtracting Twilio's clock from ours encodes skew as latency."""
    from datetime import UTC, datetime, timedelta

    ours = datetime.now(UTC)
    theirs = ours + timedelta(seconds=3)
    assert cross_process_gap(ours, theirs) is None


def test_projected_wall_clock_tracks_monotonic_offsets():
    now = mono_ns()
    later = project_utc(now + 2_000 * MS)
    earlier = project_utc(now)
    assert 1.9 < (later - earlier).total_seconds() < 2.1


def test_stopwatch_offsets_are_relative_to_its_own_origin():
    watch = Stopwatch(started_ns=1_000 * MS, started_utc=project_utc(mono_ns()))
    assert watch.offset_ms(1_250 * MS) == 250.0


# ── histograms ───────────────────────────────────────────────────────


def test_percentiles_come_off_the_histogram_not_an_average():
    hist = build([float(v) for v in range(1, 1001)])
    assert hist.count == 1000
    assert 480 <= (hist.percentile(0.50) or 0) <= 520
    assert 930 <= (hist.percentile(0.95) or 0) <= 970


def test_empty_histogram_reports_none_not_zero():
    """A zero percentile makes the least observable pipeline look fastest."""
    summary = LatencyHistogram().summary()
    assert summary["p50"] is None
    assert summary["count"] == 0
    assert summary["sufficient_samples"] is False


def test_merged_histograms_equal_the_pooled_observations():
    left = build([10.0, 20.0, 30.0])
    right = build([40.0, 50.0, 60.0])
    pooled = build([10.0, 20.0, 30.0, 40.0, 50.0, 60.0])
    left.merge(right)
    assert left.count == pooled.count
    assert left.percentile(0.5) == pooled.percentile(0.5)
    assert left.maximum == pooled.maximum


def test_under_sampled_percentiles_are_flagged():
    summary = build([100.0, 200.0]).summary(min_samples=20)
    assert summary["count"] == 2
    assert summary["sufficient_samples"] is False


def test_none_and_nan_observations_are_ignored():
    hist = build([100.0, None, float("nan")])
    assert hist.count == 1


# ── critical path ────────────────────────────────────────────────────


def test_critical_path_partitions_the_window_exactly():
    """Overlapping spans must not double count: the segments ARE the latency."""
    path = compute(
        origin_ns=0,
        target_ns=1200 * MS,
        spans=[
            PathSpan("agent.prep", 0, 100 * MS),
            PathSpan("llm.generation", 100 * MS, 1500 * MS),  # still running past first audio
            PathSpan("tool.execution", 300 * MS, 800 * MS, label="calendar"),
            PathSpan("tts.synthesis", 900 * MS, 1200 * MS),
        ],
    )
    assert path.total_ms == 1200.0
    assert sum(s.duration_ms for s in path.segments) == pytest.approx(1200.0)
    # The LLM span is 1400ms long but only owned 300ms of the response window.
    by_stage: dict[str, float] = {}
    for segment in path.segments:
        by_stage[segment.stage] = by_stage.get(segment.stage, 0.0) + segment.duration_ms
    assert by_stage["llm.generation"] == pytest.approx(300.0)
    assert by_stage["tool.execution"] == pytest.approx(500.0)
    assert by_stage["tts.synthesis"] == pytest.approx(300.0)


def test_bottleneck_is_the_measured_owner_not_the_longest_span():
    path = compute(
        origin_ns=0,
        target_ns=1200 * MS,
        spans=[
            PathSpan("llm.generation", 0, 1500 * MS),
            PathSpan("tool.execution", 100 * MS, 1100 * MS),
        ],
    )
    # The LLM span is longer, but the tool owned the window.
    assert path.bottleneck_stage == "tool.execution"
    assert path.bottleneck_ms == pytest.approx(1000.0)


def test_gaps_are_named_unattributed_rather_than_absorbed():
    path = compute(
        origin_ns=0,
        target_ns=500 * MS,
        spans=[PathSpan("llm.generation", 200 * MS, 500 * MS)],
    )
    assert path.unattributed_ms == pytest.approx(200.0)
    assert path.segments[0].stage == UNATTRIBUTED


def test_parallel_tools_each_appear_on_the_path():
    """Two tools in one iteration overlap; the later-starting one wins its slice."""
    path = compute(
        origin_ns=0,
        target_ns=600 * MS,
        spans=[
            PathSpan("llm.generation", 0, 600 * MS),
            PathSpan("tool.execution", 100 * MS, 400 * MS, span_id="a", label="calendar"),
            PathSpan("tool.execution", 150 * MS, 500 * MS, span_id="b", label="crm"),
        ],
    )
    labels = {s.label for s in path.segments if s.stage == "tool.execution"}
    assert labels == {"calendar", "crm"}
    assert sum(s.duration_ms for s in path.segments) == pytest.approx(600.0)


def test_unfinished_spans_are_clipped_to_the_target():
    path = compute(origin_ns=0, target_ns=300 * MS, spans=[PathSpan("tts.synthesis", 100 * MS, None)])
    assert sum(s.duration_ms for s in path.segments) == pytest.approx(300.0)


def test_no_first_audio_means_no_path():
    assert compute(origin_ns=100, target_ns=100, spans=[]).segments == ()


# ── content policy ───────────────────────────────────────────────────


def test_transcripts_credentials_and_tool_payloads_never_enter_telemetry():
    attrs = safe_attributes(
        {
            "tool": "calendar_create",
            "text": "I need an appointment on Tuesday",
            "transcript": "…",
            "args": {"attendee": "anna@example.com"},
            "result": "booked",
            "api_key": "sk-secret",
            "authorization": "Bearer abc",
            "duration_ms": 42,
        }
    )
    assert attrs == {"tool": "calendar_create", "duration_ms": 42}


def test_phone_numbers_are_masked_not_dropped():
    attrs = safe_attributes({"to": "+4915112345678", "from": "+493012345"})
    assert attrs["to"].startswith("+49")
    assert "15112345678" not in attrs["to"]


def test_long_strings_are_truncated():
    attrs = safe_attributes({"note": "x" * 5000})
    assert len(attrs["note"]) == 200


def test_provider_event_ids_are_deterministic_so_retries_dedupe():
    first = deterministic_event_id("call1", "call.provider_status", "3")
    second = deterministic_event_id("call1", "call.provider_status", "3")
    other = deterministic_event_id("call1", "call.provider_status", "4")
    assert first == second != other


# ── taxonomy ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("none", FailureCategory.NONE),
        ("busy", FailureCategory.CALLEE_UNAVAILABLE),
        ("no_answer", FailureCategory.CALLEE_UNAVAILABLE),
        ("blocked", FailureCategory.POLICY_DECLINED),
        ("quiet_hours", FailureCategory.POLICY_DECLINED),
        ("callee_hangup", FailureCategory.ENDED_BY_PARTY),
        ("ws_drop", FailureCategory.TECHNICAL),
        ("llm_error", FailureCategory.TECHNICAL),
        ("not_a_real_code", FailureCategory.UNKNOWN),
    ],
)
def test_failure_categories_separate_our_faults_from_everyone_elses(code, expected):
    assert categorise(code) is expected


def test_unexpected_disconnect_excludes_a_normal_hangup():
    assert is_unexpected_disconnect("ws_drop") is True
    assert is_unexpected_disconnect("callee_hangup") is False
    assert is_unexpected_disconnect("none") is False


# ── metric definitions ───────────────────────────────────────────────


def test_every_metric_documents_its_boundaries():
    for metric in METRICS.values():
        assert metric.start_event, metric.key
        assert metric.end_event, metric.key
        assert metric.label, metric.key


def test_unavailable_metrics_explain_themselves():
    for metric in METRICS.values():
        if not metric.available_on:
            assert metric.unavailable_reason, f"{metric.key} is unavailable with no explanation"


def test_caller_perceived_latency_is_never_claimed_as_measured():
    """The acceptance criterion: sent, played and heard must stay distinct."""
    perceived = METRICS["caller_perceived_latency_ms"]
    assert perceived.source is MeasurementSource.UNAVAILABLE
    assert perceived.available_on == ()
    sent = METRICS["response_latency_ms"]
    assert "SENT, not heard" in sent.limitations


def test_relay_cannot_report_tts_or_endpointing():
    assert METRICS["tts_first_audio_ms"].available("conversation_relay") is False
    assert METRICS["endpointing_ms"].available("conversation_relay") is False
    assert METRICS["tts_first_audio_ms"].available("media_streams") is True


# ── correlation context ──────────────────────────────────────────────


def test_a_call_registers_once_per_provider_id():
    first = ctxmod.register_call(provider_call_id="CA1", direction="inbound")
    second = ctxmod.register_call(provider_call_id="CA1", direction="inbound")
    assert first.call_id == second.call_id


def test_outbound_call_id_survives_learning_the_provider_id_later():
    ctx = ctxmod.register_call(provider_call_id="pending-1", direction="outbound")
    bound = ctxmod.attach_provider_call_id(ctx.call_id, "CA_real")
    assert bound is not None
    assert bound.call_id == ctx.call_id
    assert ctxmod.context_for_provider_call("CA_real").call_id == ctx.call_id


def test_rekeying_releases_the_superseded_provider_id():
    """The pre-dial key must not outlive the re-key.

    `_prune()` can only ever evict a tracked call's CURRENT provider id, so a
    key left behind here is unreachable for the life of the process.
    """
    ctx = ctxmod.register_call(provider_call_id="pending-1", direction="outbound")
    ctxmod.attach_provider_call_id(ctx.call_id, "CA_real")

    assert ctxmod.context_for_provider_call("pending-1") is None
    assert "pending-1" not in ctxmod._by_provider_id


def test_a_stale_pending_key_cannot_hand_a_later_call_the_earlier_call_id():
    """Why the orphan was a correctness bug and not only a leak.

    `register_call` is idempotent on `_by_provider_id`, so a surviving pending
    entry would return the previous call's context — two calls, one call_id.
    """
    first = ctxmod.register_call(provider_call_id="pending-same", direction="outbound")
    ctxmod.attach_provider_call_id(first.call_id, "CA_first")

    second = ctxmod.register_call(provider_call_id="pending-same", direction="outbound")
    assert second.call_id != first.call_id


def test_rebinding_the_same_provider_id_keeps_it_resolvable():
    """Guards the pop against being written as pop-then-overwrite.

    The outbound path passes the real CallSid as the trace key when the engine
    pre-registered the call, so the re-key is a same-key rebind.
    """
    ctx = ctxmod.register_call(provider_call_id="CA_known", direction="outbound")
    bound = ctxmod.attach_provider_call_id(ctx.call_id, "CA_known")

    assert bound is not None
    assert ctxmod.context_for_provider_call("CA_known").call_id == ctx.call_id


def test_sequence_numbers_order_events_inside_one_millisecond():
    ctx = ctxmod.register_call(provider_call_id="CA1", direction="inbound")
    assert [ctxmod.next_seq(ctx.call_id) for _ in range(3)] == [1, 2, 3]


def test_metric_dimensions_exclude_every_identifier():
    """High-cardinality ids belong in traces, never on a metric label."""
    ctx = ctxmod.register_call(provider_call_id="CA1", direction="inbound", engine="media_streams")
    dims = ctx.dims()
    assert "call_id" not in dims
    assert "provider_call_id" not in dims
    assert "trace_id" not in dims
    assert dims["engine"] == "media_streams"


async def test_context_propagates_into_a_spawned_task():
    """The turn runs in its own task; the binding has to survive create_task."""
    import asyncio

    ctx = ctxmod.register_call(provider_call_id="CA1", direction="inbound")
    seen: list[str] = []

    async def child() -> None:
        current = ctxmod.current_call()
        seen.append(current.call_id if current else "")

    with ctxmod.bind_call(ctx):
        await asyncio.create_task(child())

    assert seen == [ctx.call_id]
    assert ctxmod.current_call() is None
