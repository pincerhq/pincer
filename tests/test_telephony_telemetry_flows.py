"""
Representative call flows, end to end through the real store.

Each test drives a shape that has actually broken something in a voice stack:
a clean inbound call, a multi-turn streaming conversation, a slow tool, parallel
tools, a barge-in, a provider failure, an abrupt hangup, and telemetry that
arrives duplicated or out of order.

Timing is deterministic: stamps are supplied as explicit monotonic values, so
the asserted latencies are exact rather than "roughly".
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

from pincer.db import ensure_schema_current
from pincer.voice.telemetry import hooks, queries, runtime
from pincer.voice.telemetry.recorder import get_recorder
from pincer.voice.telemetry.schema import EventName, SpanName, SpanStatus
from pincer.voice.telemetry.tracer import drain_pending

MS = 1_000_000


@pytest.fixture
async def telemetry(tmp_path):
    """A live telemetry runtime against a real migrated database."""
    db_path = tmp_path / "telephony.db"
    ensure_schema_current(db_path)
    runtime.reset_for_tests(db_path=str(db_path))
    await get_recorder().start()
    yield db_path
    await runtime.shutdown()
    runtime.reset_for_tests()


async def _settle() -> None:
    """Make everything emitted so far durable."""
    await drain_pending()
    await get_recorder().flush()


def _inbound(engine: str = "media_streams"):
    return runtime.start_call(
        provider_call_id="CA_in",
        direction="inbound",
        engine=engine,
        language="de",
        from_number="+4915112345678",
        to_number="+493012345678",
    )


# ── successful inbound ───────────────────────────────────────────────


async def test_successful_inbound_call_is_fully_recorded(telemetry):
    tracer = _inbound()
    tracer.answered()
    tracer.media_open(transport="ws")
    origin = tracer.watch.started_ns

    turn = tracer.start_turn(speech_end_ns=origin)
    turn.stamp(EventName.STT_FINAL, at_ns=origin + 120 * MS)
    turn.stamp(EventName.AGENT_PREP_DONE, at_ns=origin + 160 * MS)
    turn.stamp(EventName.LLM_REQUEST, at_ns=origin + 160 * MS)
    turn.stamp(EventName.LLM_FIRST_TOKEN, at_ns=origin + 500 * MS)
    turn.stamp(EventName.TTS_REQUEST, at_ns=origin + 520 * MS)
    turn.stamp(EventName.TTS_FIRST_AUDIO, at_ns=origin + 700 * MS)
    turn.first_audio(kind="audio", at_ns=origin + 720 * MS)
    turn.stamp(EventName.LLM_DONE, at_ns=origin + 900 * MS)
    turn.finish()

    await tracer.finish(status="completed", failure_code="none", duration_s=31.0)
    await _settle()

    call = await queries.get_call(telemetry, "CA_in")
    assert call is not None
    assert call["status"] == "completed"
    assert call["failure_category"] == "none"
    assert call["turn_count"] == 1
    assert call["from_number_masked"].startswith("+49")
    assert "15112345678" not in call["from_number_masked"]

    turns = await queries.get_turns(telemetry, call["call_id"])
    assert len(turns) == 1
    row = turns[0]
    assert row["response_latency_ms"] == pytest.approx(720.0, abs=1.0)
    assert row["response_latency_source"] == "speech_end"
    assert row["llm_ttft_ms"] == pytest.approx(340.0, abs=1.0)
    assert row["tts_first_audio_ms"] == pytest.approx(180.0, abs=1.0)
    assert row["complete"] is True


async def test_successful_outbound_call_measures_setup_from_the_dial(telemetry):
    """Setup latency is dial → answer. Registration is NOT an answer."""
    tracer = runtime.start_call(
        provider_call_id="pending-out",
        direction="outbound",
        engine="media_streams",
        to_number="+4915199999999",
    )
    tracer.dial_requested(to="+4915199999999")
    dialed = tracer._dialed_ns
    tracer.provider_status("ringing", sequence="1")
    # Ringing for 4s, then the callee picks up.
    tracer._answered_ns = None
    tracer.answered()
    tracer._answered_ns = dialed + 4000 * MS

    turn = tracer.start_turn(speech_end_ns=tracer.watch.started_ns)
    turn.first_audio(kind="audio", at_ns=tracer.watch.started_ns + 400 * MS)
    turn.finish()
    await tracer.finish(status="completed", failure_code="none", duration_s=42.0)
    await _settle()

    call = await queries.get_call(telemetry, "pending-out")
    assert call["direction"] == "outbound"
    assert call["answered_at"] is not None
    assert call["setup_ms"] is not None and call["setup_ms"] >= 0
    assert call["to_number_masked"].startswith("+49")
    assert "15199999999" not in call["to_number_masked"]


async def test_a_slow_stt_finalisation_is_visible_as_its_own_stage(telemetry):
    """Deepgram held the final transcript for 900ms after the caller stopped."""
    tracer = _inbound()
    tracer.answered()
    origin = tracer.watch.started_ns
    turn = tracer.start_turn(speech_end_ns=origin)
    turn.stamp(EventName.STT_SPEECH_START, at_ns=origin - 2000 * MS)
    turn.stamp(EventName.STT_PARTIAL, at_ns=origin - 1700 * MS)
    turn.stamp(EventName.STT_SPEECH_END, at_ns=origin)
    turn.stamp(EventName.ENDPOINT_DECISION, at_ns=origin + 900 * MS)
    turn.stamp(EventName.STT_FINAL, at_ns=origin + 900 * MS)
    turn.first_audio(kind="audio", at_ns=origin + 1400 * MS)
    turn.finish()
    await tracer.finish(status="completed", failure_code="none", duration_s=18.0)
    await _settle()

    call = await queries.get_call(telemetry, "CA_in")
    row = (await queries.get_turns(telemetry, call["call_id"]))[0]
    assert row["endpointing_ms"] == pytest.approx(900.0, abs=1.0)
    assert row["stt_final_ms"] == pytest.approx(900.0, abs=1.0)
    assert row["stt_first_partial_ms"] == pytest.approx(300.0, abs=1.0)
    # The endpointing wait is INSIDE the response latency, which is the point:
    # it is what the caller actually waited through.
    assert row["response_latency_ms"] == pytest.approx(1400.0, abs=1.0)
    # Nothing was attributed to a stage, so it is named rather than hidden.
    assert row["critical_path"]["unattributed_ms"] == pytest.approx(1400.0, abs=1.0)


async def test_relay_turn_is_labelled_as_starting_from_transcript_arrival(telemetry):
    """ConversationRelay cannot see speech end; the number must say so."""
    tracer = _inbound(engine="conversation_relay")
    tracer.answered()
    turn = tracer.start_turn()  # no speech_end_ns — the engine has none
    turn.first_audio(kind="text_token")
    turn.finish()
    await tracer.finish(status="completed", failure_code="none", duration_s=10.0)
    await _settle()

    call = await queries.get_call(telemetry, "CA_in")
    row = (await queries.get_turns(telemetry, call["call_id"]))[0]
    assert row["response_latency_source"] == "transcript_arrival"


# ── multi-turn streaming ─────────────────────────────────────────────


async def test_multi_turn_conversation_records_each_turn(telemetry):
    tracer = _inbound()
    tracer.answered()
    for index in range(3):
        origin = tracer.watch.started_ns + index * 10_000 * MS
        turn = tracer.start_turn(speech_end_ns=origin)
        turn.stamp(EventName.LLM_REQUEST, at_ns=origin + 50 * MS)
        turn.first_audio(kind="audio", at_ns=origin + (300 + index * 100) * MS)
        turn.finish()
    await tracer.finish(status="completed", failure_code="none", duration_s=45.0)
    await _settle()

    call = await queries.get_call(telemetry, "CA_in")
    turns = await queries.get_turns(telemetry, call["call_id"])
    assert [t["turn_no"] for t in turns] == [1, 2, 3]
    assert [round(t["response_latency_ms"]) for t in turns] == [300, 400, 500]
    assert call["turn_count"] == 3


# ── slow stages ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("stage", "span_name"),
    [("llm", SpanName.LLM), ("tts", SpanName.TTS), ("tool", SpanName.TOOL)],
)
async def test_a_slow_stage_is_named_as_the_bottleneck(telemetry, stage, span_name):
    tracer = _inbound()
    tracer.answered()
    origin = tracer.watch.started_ns
    turn = tracer.start_turn(speech_end_ns=origin)

    slow = turn.open_span(span_name, label=stage)
    slow.start_ns = origin + 50 * MS
    slow.close()
    # Close it deterministically at +2050ms.
    slow_record = tracer._spans[-1]
    slow_record.end_mono_ns = origin + 2050 * MS
    slow_record.duration_ms = 2000.0

    turn.first_audio(kind="audio", at_ns=origin + 2100 * MS)
    turn.finish()
    await tracer.finish(status="completed", failure_code="none", duration_s=20.0)
    await _settle()

    call = await queries.get_call(telemetry, "CA_in")
    row = (await queries.get_turns(telemetry, call["call_id"]))[0]
    assert row["bottleneck_stage"] == str(span_name)
    assert row["bottleneck_ms"] == pytest.approx(2000.0, abs=1.0)
    assert row["response_latency_ms"] == pytest.approx(2100.0, abs=1.0)


async def test_parallel_tool_calls_are_separate_spans(telemetry):
    tracer = _inbound()
    tracer.answered()
    origin = tracer.watch.started_ns
    turn = tracer.start_turn(speech_end_ns=origin)

    for name, start, end in (("calendar", 100, 800), ("crm", 120, 950)):
        span = turn.open_span(SpanName.TOOL, tool=name)
        span.start_ns = origin + start * MS
        span.close()
        tracer._spans[-1].end_mono_ns = origin + end * MS
        tracer._spans[-1].duration_ms = float(end - start)
        turn.bump("tool_calls")

    turn.first_audio(kind="audio", at_ns=origin + 1000 * MS)
    turn.finish()
    await tracer.finish(status="completed", failure_code="none", duration_s=15.0)
    await _settle()

    call = await queries.get_call(telemetry, "CA_in")
    spans = await queries.get_spans(telemetry, call["call_id"])
    tools = [s for s in spans if s["name"] == str(SpanName.TOOL)]
    assert {s["attributes"]["tool"] for s in tools} == {"calendar", "crm"}

    row = (await queries.get_turns(telemetry, call["call_id"]))[0]
    assert row["tool_calls"] == 2
    # Overlapping tools must not be summed into more than the window.
    segments = row["critical_path"]["segments"]
    assert sum(s["duration_ms"] for s in segments) == pytest.approx(1000.0, abs=1.0)


async def test_a_tool_retry_is_its_own_attempt_span(telemetry):
    tracer = _inbound()
    tracer.answered()
    turn = tracer.start_turn(speech_end_ns=tracer.watch.started_ns)
    turn.open_span(SpanName.TOOL, tool="crm", attempt=1).close(SpanStatus.ERROR)
    turn.open_span(SpanName.TOOL, tool="crm", attempt=2).close(SpanStatus.OK)
    turn.bump("tool_retries")
    turn.first_audio(kind="audio")
    turn.finish()
    await tracer.finish(status="completed", failure_code="none", duration_s=12.0)
    await _settle()

    call = await queries.get_call(telemetry, "CA_in")
    spans = await queries.get_spans(telemetry, call["call_id"])
    attempts = sorted(s["attempt"] for s in spans if s["name"] == str(SpanName.TOOL))
    assert attempts == [1, 2]
    row = (await queries.get_turns(telemetry, call["call_id"]))[0]
    assert row["tool_retries"] == 1


# ── interruption and cancellation ────────────────────────────────────


async def test_a_barge_in_turn_is_cancelled_not_counted_as_slow(telemetry):
    tracer = _inbound()
    tracer.answered()
    first = tracer.start_turn(speech_end_ns=tracer.watch.started_ns)
    first.cancel("barge_in")

    second = tracer.start_turn(speech_end_ns=tracer.watch.started_ns + 1000 * MS)
    second.first_audio(kind="audio", at_ns=tracer.watch.started_ns + 1300 * MS)
    second.finish()
    await tracer.finish(status="completed", failure_code="none", duration_s=25.0)
    await _settle()

    call = await queries.get_call(telemetry, "CA_in")
    turns = await queries.get_turns(telemetry, call["call_id"])
    cancelled = [t for t in turns if t["cancelled"]]
    assert len(cancelled) == 1
    assert cancelled[0]["interrupted"] is True
    # A cancelled turn has no response latency and must not pollute percentiles.
    assert cancelled[0]["response_latency_ms"] is None
    assert cancelled[0]["complete"] is False

    aggregate = await queries.overview(telemetry, queries.CallFilters.for_hours(1))
    assert aggregate.stages["response_latency_ms"]["count"] == 1


# ── failure paths ────────────────────────────────────────────────────


async def test_a_call_that_never_connected_still_has_a_diagnosable_row(telemetry):
    tracer = runtime.start_call(
        provider_call_id="pending-x",
        direction="outbound",
        engine="conversation_relay",
        to_number="+4915199999999",
    )
    tracer.dial_requested(to="+4915199999999")
    tracer.provider_status("ringing", sequence="1")
    tracer.provider_status("no-answer", sequence="2")
    await tracer.finish(status="failed", failure_code="no_answer", termination_reason="timeout_ringing")
    await _settle()

    call = await queries.get_call(telemetry, "pending-x")
    assert call is not None
    assert call["answered_at"] is None
    assert call["failure_category"] == "callee_unavailable"
    assert call["termination_reason"] == "timeout_ringing"

    events = await queries.get_events(telemetry, call["call_id"])
    assert [e["name"] for e in events if e["name"] == "call.provider_status"]


async def test_an_abrupt_hangup_keeps_the_partial_turn(telemetry):
    """The call dies mid-turn: whatever closed must survive."""
    tracer = _inbound()
    tracer.answered()
    turn = tracer.start_turn(speech_end_ns=tracer.watch.started_ns)
    turn.open_span(SpanName.LLM).close()
    # No first audio: the caller hung up while the model was still writing.
    turn.fail("caller_hangup")
    await tracer.finish(status="failed", failure_code="ws_drop", termination_reason="socket_closed")
    await _settle()

    call = await queries.get_call(telemetry, "CA_in")
    assert call["failure_category"] == "technical"
    row = (await queries.get_turns(telemetry, call["call_id"]))[0]
    assert row["response_latency_ms"] is None
    assert row["complete"] is False
    assert row["error"] == "caller_hangup"
    spans = await queries.get_spans(telemetry, call["call_id"])
    assert any(s["name"] == str(SpanName.LLM) for s in spans)


async def test_provider_failure_and_reconnect_are_counted(telemetry):
    tracer = _inbound()
    tracer.answered()
    tracer.reconnect("stt", attempt=1)
    tracer.error("stt_error", stage="stt")
    tracer.timeout("tool", limit_s=10.0, tool="calendar")
    await tracer.finish(status="failed", failure_code="stt_error")
    await _settle()

    call = await queries.get_call(telemetry, "CA_in")
    assert call["reconnect_count"] == 1
    assert call["error_count"] == 1
    assert call["timeout_count"] == 1

    aggregate = await queries.overview(telemetry, queries.CallFilters.for_hours(1))
    assert aggregate.reliability["reconnects"] == 1
    assert aggregate.reliability["timeouts"] == 1


async def test_open_spans_are_closed_when_the_call_dies_under_them(telemetry):
    tracer = _inbound()
    tracer.answered()
    tracer.open_span(SpanName.MEDIA, key="media")
    await tracer.finish(status="failed", failure_code="ws_drop")
    await _settle()

    call = await queries.get_call(telemetry, "CA_in")
    spans = await queries.get_spans(telemetry, call["call_id"])
    media = [s for s in spans if s["name"] == str(SpanName.MEDIA)]
    assert media and media[0]["status"] == "cancelled"


# ── duplicate / out-of-order / missing telemetry ─────────────────────


async def test_duplicate_provider_callbacks_collapse_to_one_event(telemetry):
    tracer = _inbound()
    for _ in range(4):
        tracer.provider_status("completed", sequence="7")
    await tracer.finish(status="completed", failure_code="none")
    await _settle()

    call = await queries.get_call(telemetry, "CA_in")
    events = await queries.get_events(telemetry, call["call_id"])
    statuses = [e for e in events if e["name"] == "call.provider_status"]
    assert len(statuses) == 1


async def test_out_of_order_events_are_ordered_at_read_time(telemetry):
    """A Twilio callback can land minutes after teardown; it must sort into place."""
    tracer = _inbound()
    tracer.answered()
    await tracer.finish(status="completed", failure_code="none")
    await _settle()

    call = await queries.get_call(telemetry, "CA_in")
    # Inject a late-arriving event stamped BEFORE the ones already stored.
    from pincer.db.engine import get_database_url, get_engine

    async with get_engine(get_database_url(Path(str(telemetry)))).connect() as db:
        await db.execute(
            sa.text(
                "INSERT INTO pincer_telephony_events (event_id, call_id, name, ts_utc, mono_ns, seq) "
                "VALUES (:event_id, :call_id, :name, :ts_utc, :mono_ns, :seq)"
            ),
            {
                "event_id": "late-1",
                "call_id": call["call_id"],
                "name": "call.provider_status",
                "ts_utc": "1990-01-01T00:00:00+00:00",
                "mono_ns": 0,
                "seq": 999,
            },
        )
        await db.commit()

    events = await queries.get_events(telemetry, call["call_id"])
    assert events[0]["event_id"] == "late-1"
    timestamps = [e["ts_utc"] for e in events]
    assert timestamps == sorted(timestamps)


async def test_an_unsampled_call_keeps_its_lifecycle_but_not_its_turns(telemetry):
    """Sampling drops detail, never the call's own story."""
    runtime.reset_for_tests(db_path=str(telemetry), sample=0.0)
    await get_recorder().start()

    tracer = runtime.start_call(provider_call_id="CA_unsampled", direction="inbound", engine="media_streams")
    assert tracer is not None
    tracer.answered()
    turn = tracer.start_turn(speech_end_ns=tracer.watch.started_ns)
    turn.first_audio(kind="audio")
    turn.finish()
    await tracer.finish(status="failed", failure_code="llm_error")
    await _settle()

    call = await queries.get_call(telemetry, "CA_unsampled")
    assert call is not None
    assert call["sampled"] is False
    assert call["failure_code"] == "llm_error"
    assert call["coverage"] == "lifecycle_only"
    assert call["turn_count"] == 1  # counted, even though the detail was dropped
    assert await queries.get_turns(telemetry, call["call_id"]) == []

    events = await queries.get_events(telemetry, call["call_id"])
    names = {e["name"] for e in events}
    assert "call.ended" in names
    assert "turn.start" not in names


async def test_missing_telemetry_is_reported_as_coverage_not_hidden(telemetry):
    tracer = _inbound()
    tracer.answered()
    turn = tracer.start_turn(speech_end_ns=tracer.watch.started_ns)
    turn.fail("llm_error")  # no first audio -> no response latency
    await tracer.finish(status="failed", failure_code="llm_error")
    await _settle()

    aggregate = await queries.overview(telemetry, queries.CallFilters.for_hours(1))
    assert aggregate.coverage["turns"] == 1
    assert aggregate.coverage["turns_without_response_latency"] == 1
    assert aggregate.stages["response_latency_ms"]["count"] == 0
    assert aggregate.stages["response_latency_ms"]["p50"] is None


# ── media establishment ──────────────────────────────────────────────

# Both TwiML handlers open the socket and only then mark the answer: the same
# wire frame carries both, and the answer costs an await. Tests that call
# `answered()` first exercise the order production never uses.


async def test_the_socket_opening_before_the_answer_measures_zero_not_null(telemetry):
    """The production order. The open IS the pickup, so there is no interval."""
    tracer = _inbound()
    tracer.media_open(transport="ws")
    tracer.answered()
    await tracer.finish(status="completed", failure_code="none")
    await _settle()

    call = await queries.get_call(telemetry, "CA_in")
    assert call is not None
    assert call["media_open_at"] is not None
    assert call["media_establish_ms"] == 0.0


async def test_an_answer_that_precedes_the_socket_measures_the_real_gap(telemetry):
    """ConversationRelay's HTTP `setup` webhook can beat its own socket."""
    tracer = _inbound(engine="conversation_relay")
    tracer.answered()
    tracer.media_open(transport="ws")
    await tracer.finish(status="completed", failure_code="none")
    await _settle()

    call = await queries.get_call(telemetry, "CA_in")
    assert call is not None
    assert call["media_establish_ms"] is not None
    assert call["media_establish_ms"] >= 0.0


async def test_a_mid_call_reconnect_never_rewrites_the_establishment(telemetry):
    """Back from a <Dial> transfer: a second open is a timeline event only.

    Measuring it against the answer would subtract the whole conversation —
    and measuring the answer against the *first* open went negative.
    """
    tracer = _inbound()
    tracer.media_open(transport="ws")
    tracer.answered()
    turn = tracer.start_turn(speech_end_ns=tracer.watch.started_ns)
    turn.first_audio(kind="audio", at_ns=tracer.watch.started_ns + 400 * MS)
    turn.finish()
    tracer.media_open(transport="ws")  # the socket came back
    await tracer.finish(status="completed", failure_code="none", duration_s=42.0)
    await _settle()

    call = await queries.get_call(telemetry, "CA_in")
    assert call is not None
    assert call["media_establish_ms"] == 0.0

    opens = [e for e in await queries.get_events(telemetry, call["call_id"]) if e["name"] == "call.media_stream_open"]
    assert len(opens) == 2
    assert opens[1]["ts_utc"] >= opens[0]["ts_utc"]


# ── aggregation ──────────────────────────────────────────────────────


async def test_rates_carry_their_denominators_and_exclude_policy_declines(telemetry):
    declined = runtime.start_call(provider_call_id="CA_blocked", direction="inbound", engine="media_streams")
    await declined.finish(status="failed", failure_code="blocked")

    connected = runtime.start_call(provider_call_id="CA_ok", direction="inbound", engine="media_streams")
    connected.answered()
    await connected.finish(status="completed", failure_code="none")

    broken = runtime.start_call(provider_call_id="CA_bad", direction="inbound", engine="media_streams")
    broken.answered()
    await broken.finish(status="failed", failure_code="ws_drop")
    await _settle()

    aggregate = await queries.overview(telemetry, queries.CallFilters.for_hours(1))
    assert aggregate.calls["declined_by_policy"] == 1
    assert aggregate.calls["attempted"] == 2
    # Connection rate ignores the policy decline entirely.
    assert aggregate.rates["connection_rate"]["denominator"] == 2
    assert aggregate.rates["connection_rate"]["value"] == pytest.approx(1.0)
    # Technical failure rate counts the ws_drop, not the block.
    assert aggregate.rates["technical_failure_rate"]["numerator"] == 1
    assert aggregate.rates["unexpected_disconnect_rate"]["numerator"] == 1


async def test_empty_window_reports_none_rather_than_zero_percent(telemetry):
    aggregate = await queries.overview(telemetry, queries.CallFilters.for_hours(1))
    assert aggregate.rates["connection_rate"]["value"] is None
    assert aggregate.calls["total"] == 0


async def test_engine_comparison_keeps_groups_separate(telemetry):
    for sid, engine, latency in (("CA_a", "media_streams", 300), ("CA_b", "conversation_relay", 1500)):
        tracer = runtime.start_call(provider_call_id=sid, direction="inbound", engine=engine)
        tracer.answered()
        turn = tracer.start_turn(speech_end_ns=tracer.watch.started_ns)
        turn.first_audio(kind="audio", at_ns=tracer.watch.started_ns + latency * MS)
        turn.finish()
        await tracer.finish(status="completed", failure_code="none")
    await _settle()

    aggregate = await queries.overview(telemetry, queries.CallFilters.for_hours(1))
    by_engine = {row["key"]: row for row in aggregate.comparisons["engine"]}
    assert by_engine["media_streams"]["p50"] < by_engine["conversation_relay"]["p50"]
    # Both are under-sampled; the UI must be told so.
    assert all(not row["sufficient_samples"] for row in by_engine.values())


async def test_unavailable_metrics_are_listed_for_the_engines_present(telemetry):
    tracer = _inbound(engine="conversation_relay")
    tracer.answered()
    await tracer.finish(status="completed", failure_code="none")
    await _settle()

    aggregate = await queries.overview(telemetry, queries.CallFilters.for_hours(1))
    keys = {entry["key"] for entry in aggregate.unavailable}
    assert "tts_first_audio_ms" in keys
    assert "endpointing_ms" in keys
    assert "caller_perceived_latency_ms" in keys
    for entry in aggregate.unavailable:
        assert entry["unavailable_reason"]


async def test_a_policy_declined_call_is_terminal_immediately(telemetry):
    """It never reaches the engine, so nothing else would ever close its row."""
    from pincer.voice.telemetry import hooks

    await hooks.call_declined("CA_declined", failure_code="blocked", reason="receptionist_policy", language="de")
    await _settle()

    call = await queries.get_call(telemetry, "CA_declined")
    assert call is not None
    assert call["status"] == "failed"
    assert call["outcome"] == "declined"
    assert call["failure_category"] == "policy_declined"
    assert call["ended_at"]
    # The tracer is released, so the decline cannot leak into a later call.
    assert runtime.tracer_for("CA_declined") is None

    aggregate = await queries.overview(telemetry, queries.CallFilters.for_hours(1))
    assert aggregate.calls["declined_by_policy"] == 1
    assert aggregate.rates["technical_failure_rate"]["numerator"] == 0


async def test_a_rejected_dial_is_terminal(telemetry):
    """Twilio refusing the dial is a technical failure, not a call still in progress."""
    for key in ("pending-aaa", "pending-bbb"):  # what uuid4() yields per ATTEMPT
        hooks.dial_requested(key, to_number="+4930111", engine="media_streams", language="de")
        await hooks.dial_rejected(key, error="TwilioRestException: 21211")
    await _settle()

    aggregate = await queries.overview(telemetry, queries.CallFilters.for_hours(1))
    assert aggregate.calls["active"] == 0
    assert aggregate.rates["technical_failure_rate"]["denominator"] == 2
    assert aggregate.rates["technical_failure_rate"]["numerator"] == 2


async def test_a_rejected_dial_closes_even_when_the_write_backlog_is_full(telemetry, monkeypatch):
    """The terminal write is awaited, not spawned: a full backlog drops spawned
    writes, and no status callback would ever arrive to close this row later."""
    from pincer.voice.telemetry import tracer as tracer_mod

    hooks.dial_requested("pending-ccc", to_number="+4930111", engine="media_streams", language="de")
    await _settle()
    monkeypatch.setattr(tracer_mod, "_MAX_PENDING", 0)  # every spawned write is now dropped
    await hooks.dial_rejected("pending-ccc", error="TwilioRestException: 21211")
    await _settle()

    aggregate = await queries.overview(telemetry, queries.CallFilters.for_hours(1))
    assert aggregate.calls["active"] == 0
    assert aggregate.rates["technical_failure_rate"]["numerator"] == 1


# ── a closed row stays closed ────────────────────────────────────────
#
# Twilio retries status callbacks for minutes, and they can arrive out of
# order. Every one of these drives a write that used to reopen an ended call.


async def test_a_late_first_answer_cannot_reopen_an_ended_call(telemetry):
    """The call ended before pickup; the answer callback lands afterwards.

    `answered()`'s own `_answered_ns` guard does NOT cover this: the call was
    never answered, so that field is still None and the guard passes.
    """
    tracer = _inbound()
    await tracer.finish(status="failed", failure_code="no_answer", duration_s=8.0)
    tracer.answered()
    await _settle()

    call = await queries.get_call(telemetry, "CA_in")
    assert call["status"] == "failed"
    assert call["failure_code"] == "no_answer"


async def test_a_late_registration_from_a_fresh_tracer_cannot_reopen(telemetry):
    """The case an in-memory `_finished` flag cannot catch.

    Tracers are evicted (LRU at `_MAX_TRACERS`, a 900 s context TTL) and the
    process can restart, after which a late webhook builds a NEW tracer whose
    `_finished` is False — `hooks.call_declined` does exactly this. Ordering
    therefore has to be a property of the row, not of the Python object.
    """
    tracer = _inbound()
    tracer.answered()
    await tracer.finish(status="completed", duration_s=30.0)
    await _settle()

    runtime.forget("CA_in")
    revenant = _inbound()
    assert revenant is not tracer
    assert revenant._finished is False  # the flag genuinely does not help here
    revenant.registered()
    revenant.answered()
    await _settle()

    call = await queries.get_call(telemetry, "CA_in")
    assert call["status"] == "completed"


async def test_a_live_status_can_still_advance(telemetry):
    """The absorbing rule must not freeze a call that is merely in progress."""
    tracer = _inbound()
    tracer.registered()
    await _settle()
    assert (await queries.get_call(telemetry, "CA_in"))["status"] == "active"

    tracer.answered()
    await _settle()
    assert (await queries.get_call(telemetry, "CA_in"))["status"] == "connected"

    await tracer.finish(status="completed", duration_s=12.0)
    await _settle()
    assert (await queries.get_call(telemetry, "CA_in"))["status"] == "completed"


async def test_every_turn_query_returns_real_booleans(telemetry):
    """One shape for `complete`/`interrupted`/`cancelled`, whichever query served it.

    SQLite stores these as 0/1. The dashboard declares them `boolean`
    (`TelephonyTurn` in api/types.ts) and types three different endpoints with
    that one interface, so a query that forwards the raw integer satisfies the
    type at run time right up until the first `=== true`.

    Asserted across all three rather than only the one that had drifted, so a
    fourth turn-returning query cannot quietly reintroduce it.
    """
    tracer = _inbound()
    tracer.answered()
    turn = tracer.start_turn(speech_end_ns=tracer.watch.started_ns)
    turn.first_audio(kind="audio", at_ns=tracer.watch.started_ns + 400 * MS)
    turn.finish()
    await tracer.finish(status="completed", duration_s=20.0)
    await _settle()

    window = queries.CallFilters.for_hours(1)
    sources = {
        "get_turns": await queries.get_turns(telemetry, tracer.ctx.call_id),
        "slowest_turns": await queries.slowest_turns(telemetry, window),
        "export_turns": await queries.export_turns(telemetry, window),
    }

    for name, turns in sources.items():
        assert turns, f"{name} returned no turns"
        for row in turns:
            for field in ("complete", "interrupted", "cancelled"):
                assert isinstance(row[field], bool), f"{name}.{field} is {type(row[field]).__name__}, not bool"
