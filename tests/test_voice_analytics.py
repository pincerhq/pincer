"""Conversation analytics — talk time, interruptions, sentiment.

The measurement half is arithmetic and is tested as such. The sentiment half
is tested for the property that actually matters: it describes the call, and
never the person. A label without grounding is the failure mode, not a
mislabelled call.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import aiosqlite
import pytest

from pincer.voice import analytics as an
from pincer.voice.analytics import (
    METHOD_ESTIMATED,
    METHOD_EXACT,
    REASON_EXTRACTION_FAILED,
    REASON_NOT_CONVERSED,
    REASON_TOO_SHORT,
    CallAnalytics,
    TalkTimeAccumulator,
)
from pincer.voice.outcome import CallOutcome

# 8000 μ-law bytes = one second of audio.
ONE_SECOND = 8000


class FakeClock:
    """Manual monotonic clock in seconds."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


# ── Media Streams: exact ─────────────────────────────────────────────


def test_talktime_media_streams_exact_from_chunks():
    """μ-law byte counts are the measurement: 8000 bytes = 1000 ms."""
    clock = FakeClock()
    acc = TalkTimeAccumulator(method=METHOD_EXACT, clock=clock)

    acc.agent_audio_begin()
    acc.agent_audio_bytes(ONE_SECOND)  # 1.0 s
    acc.agent_audio_bytes(ONE_SECOND // 2)  # 0.5 s
    clock.advance(1.5)

    acc.caller_span(2.0, 5.0)  # 3.0 s of caller
    clock.advance(3.5)

    result = acc.finalize(duration_ms=10_000)
    assert result.method == METHOD_EXACT
    assert result.agent_speech_ms == 1500
    assert result.caller_speech_ms == 3000
    assert result.overlap_ms == 0
    assert result.silence_ms == 10_000 - 4500
    assert result.talk_ratio == pytest.approx(1500 / 4500, abs=1e-4)


def test_barge_in_subtracts_cancelled_audio():
    """Audio Twilio never played is not agent speech.

    Without this the ratio is inflated precisely on the calls where the caller
    was cutting in — the calls where an honest ratio matters most.
    """
    clock = FakeClock()
    acc = TalkTimeAccumulator(method=METHOD_EXACT, clock=clock)

    acc.agent_audio_begin()
    acc.agent_audio_bytes(ONE_SECOND * 10)  # ten seconds queued instantly
    clock.advance(2.0)  # only two seconds had time to play
    acc.agent_audio_cancelled()

    result = acc.finalize(duration_ms=10_000)
    assert result.agent_speech_ms == 2000, "cancelled remainder must not count"
    assert result.interruptions == 0  # cancellation and barge-in count are separate signals


def test_uncancelled_audio_counts_in_full():
    clock = FakeClock()
    acc = TalkTimeAccumulator(method=METHOD_EXACT, clock=clock)
    acc.agent_audio_begin()
    acc.agent_audio_bytes(ONE_SECOND * 3)
    result = acc.finalize(duration_ms=10_000)
    assert result.agent_speech_ms == 3000


def test_a_new_utterance_closes_the_previous_one():
    clock = FakeClock()
    acc = TalkTimeAccumulator(method=METHOD_EXACT, clock=clock)
    acc.agent_audio_begin()
    acc.agent_audio_bytes(ONE_SECOND)
    clock.advance(1.0)
    acc.agent_audio_begin()
    acc.agent_audio_bytes(ONE_SECOND * 2)
    result = acc.finalize(duration_ms=10_000)
    assert result.agent_speech_ms == 3000


def test_overlap_counts_both():
    """Both speakers are credited; nobody arbitrates who had the floor."""
    clock = FakeClock()
    acc = TalkTimeAccumulator(method=METHOD_EXACT, clock=clock)

    acc.agent_audio_begin()  # agent 0.0 → 4.0 s
    acc.agent_audio_bytes(ONE_SECOND * 4)
    acc.caller_span(3.0, 6.0)  # caller 3.0 → 6.0 s, overlapping 1 s

    result = acc.finalize(duration_ms=10_000)
    assert result.agent_speech_ms == 4000
    assert result.caller_speech_ms == 3000
    assert result.overlap_ms == 1000
    # Silence is the time NOBODY spoke, so the union is what gets subtracted:
    # 4000 + 3000 - 1000 = 6000 spoken.
    assert result.silence_ms == 4000


def test_silence_never_goes_negative():
    clock = FakeClock()
    acc = TalkTimeAccumulator(method=METHOD_EXACT, clock=clock)
    acc.agent_audio_begin()
    acc.agent_audio_bytes(ONE_SECOND * 30)
    result = acc.finalize(duration_ms=5_000)
    assert result.silence_ms == 0


# ── ConversationRelay: estimated ─────────────────────────────────────


def test_cr_estimation_rates_per_language():
    """German is slower per character than English, so the same text is longer."""
    text = "x" * 155
    assert an.estimate_speech_ms(text, "en") == pytest.approx(10_000, rel=0.01)
    assert an.estimate_speech_ms(text, "de") > an.estimate_speech_ms(text, "en")
    assert an.estimate_speech_ms("", "en") == 0.0
    # An unknown language falls back rather than dividing by zero.
    assert an.estimate_speech_ms(text, "xx") == an.estimate_speech_ms(text, "en")


def test_cr_accumulates_both_sides_as_estimates():
    clock = FakeClock()
    acc = TalkTimeAccumulator(method=METHOD_ESTIMATED, clock=clock)
    acc.agent_text("x" * 155, "en")  # ~10 s
    clock.advance(10.0)
    acc.caller_text("y" * 155, "en")  # ~10 s, anchored backwards from now
    clock.advance(1.0)

    result = acc.finalize(duration_ms=30_000)
    assert result.method == METHOD_ESTIMATED
    assert result.agent_speech_ms == pytest.approx(10_000, abs=200)
    assert result.caller_speech_ms == pytest.approx(10_000, abs=200)
    assert result.talk_ratio == pytest.approx(0.5, abs=0.02)


def test_engines_declare_their_own_method():
    """Provenance is a property of the engine, not a label applied later."""
    from pincer.voice.engine import ConversationRelayEngine, MediaStreamEngine

    assert ConversationRelayEngine.analytics_method == METHOD_ESTIMATED
    assert MediaStreamEngine.analytics_method == METHOD_EXACT


def test_interruptions_counted_on_both_engines():
    acc = TalkTimeAccumulator(method=METHOD_ESTIMATED, clock=FakeClock())
    acc.interruption()
    acc.interruption()
    assert acc.finalize(duration_ms=1000).interruptions == 2


# ── Null states ──────────────────────────────────────────────────────


def test_voicemail_null_analytics():
    """A call nobody had gets a row saying so — not zeros, not "neutral"."""
    acc = TalkTimeAccumulator(method=METHOD_EXACT, clock=FakeClock())
    acc.agent_audio_begin()
    acc.agent_audio_bytes(ONE_SECOND * 5)

    result = acc.finalize(duration_ms=20_000, conversed=False)
    assert result.agent_speech_ms is None
    assert result.caller_speech_ms is None
    assert result.silence_ms is None
    assert result.talk_ratio is None
    assert result.sentiment_reason == REASON_NOT_CONVERSED

    an.apply_sentiment(result, CallOutcome(outcome="voicemail", sentiment="neutral"))
    assert result.sentiment is None, "a call that never happened has no sentiment"


@pytest.mark.parametrize(
    ("outcome_code", "failure_code", "expected"),
    [
        ("voicemail", "", False),
        ("no_answer", "", False),
        ("completed", "", True),
        ("", "voicemail", False),
        ("", "no_answer", False),
        ("", "none", True),
        ("", "llm_error", True),  # the call happened; it just went badly
    ],
)
def test_was_conversational(outcome_code, failure_code, expected):
    assert an.was_conversational(outcome_code=outcome_code, failure_code=failure_code) is expected


def test_too_short_sentiment_null():
    """Under ten seconds of speech there is nothing to read a stance from."""
    short = CallAnalytics(agent_speech_ms=3000, caller_speech_ms=2000, method=METHOD_EXACT)
    an.apply_sentiment(short, CallOutcome(sentiment="positive", sentiment_rationale="thanked us"))
    assert short.sentiment is None
    assert short.sentiment_reason == REASON_TOO_SHORT

    long_enough = CallAnalytics(agent_speech_ms=8000, caller_speech_ms=9000, method=METHOD_EXACT)
    an.apply_sentiment(long_enough, CallOutcome(sentiment="positive", sentiment_rationale="thanked us twice"))
    assert long_enough.sentiment == "positive"
    assert long_enough.sentiment_reason == ""


def test_failed_extraction_is_labelled_not_neutralised():
    record = CallAnalytics(agent_speech_ms=20_000, caller_speech_ms=20_000, method=METHOD_EXACT)
    an.apply_sentiment(record, None)
    assert record.sentiment is None
    assert record.sentiment_reason == REASON_EXTRACTION_FAILED

    record2 = CallAnalytics(agent_speech_ms=20_000, caller_speech_ms=20_000, method=METHOD_EXACT)
    an.apply_sentiment(record2, CallOutcome(sentiment="euphoric"))  # not a valid label
    assert record2.sentiment is None
    assert record2.sentiment_reason == REASON_EXTRACTION_FAILED


# ── Sentiment in the extraction schema ───────────────────────────────


def test_sentiment_parsed_from_the_outcome_pass():
    payload = json.dumps(
        {
            "outcome": "completed",
            "task_result": "Booked.",
            "key_facts": [],
            "commitments": [],
            "follow_up_suggestions": [],
            "language": "en",
            "sentiment": "negative",
            "sentiment_trajectory": "declining",
            "sentiment_rationale": "Said the third delay was unacceptable.",
        }
    )
    outcome = CallOutcome.from_json(payload)
    assert outcome is not None
    assert outcome.sentiment == "negative"
    assert outcome.sentiment_trajectory == "declining"
    assert outcome.sentiment_rationale == "Said the third delay was unacceptable."
    # It round-trips, so the audit action row carries it too.
    assert json.loads(outcome.to_json())["sentiment"] == "negative"


def test_a_label_without_a_rationale_is_dropped():
    """A bare label invites over-trust; the pair travels together or not at all."""
    payload = json.dumps(
        {
            "outcome": "completed",
            "task_result": "x",
            "language": "en",
            "sentiment": "bogus",
            "sentiment_rationale": "Sounded cross.",
        }
    )
    outcome = CallOutcome.from_json(payload)
    assert outcome is not None
    assert outcome.sentiment is None
    assert outcome.sentiment_rationale is None
    assert outcome.sentiment_trajectory is None


def test_missing_sentiment_fields_are_none_not_neutral():
    """A Sprint-3-era response (no sentiment keys at all) must not become neutral."""
    payload = json.dumps({"outcome": "completed", "task_result": "x", "language": "en"})
    outcome = CallOutcome.from_json(payload)
    assert outcome is not None
    assert outcome.sentiment is None


def test_extraction_prompt_forbids_character_judgements():
    """The rule that keeps this analytics and not profiling."""
    from pincer.voice.outcome import EXTRACTION_SYSTEM_PROMPT

    lowered = EXTRACTION_SYSTEM_PROMPT.lower()
    assert "an angry person" in lowered  # the forbidden example is spelled out
    assert "personality" in lowered
    assert "caller's stance" in lowered
    assert "no clear signals" in lowered


class _CountingLLM:
    """Counts completions so "one LLM pass" is asserted, not assumed."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    async def complete(self, messages, tools=None, model=None, max_tokens=None, temperature=None, system=None):
        self.calls += 1
        self.last_user = messages[0].content
        return SimpleNamespace(content=self.content)


async def test_sentiment_schema_in_outcome_pass():
    """Sentiment costs zero additional LLM calls — it rides on Sprint 3's."""
    from pincer.voice.outcome import extract_outcome

    llm = _CountingLLM(
        json.dumps(
            {
                "outcome": "completed",
                "task_result": "The appointment was confirmed.",
                "key_facts": [],
                "commitments": [],
                "follow_up_suggestions": [],
                "language": "en",
                "sentiment": "positive",
                "sentiment_trajectory": "stable",
                "sentiment_rationale": "Thanked the agent for confirming quickly.",
            }
        )
    )
    outcome = await extract_outcome(
        llm,
        "Agent: confirming your appointment.\nCaller: thanked the agent for confirming quickly.",
        "",
        language="en",
        talk_time="SPEAKING TIME (exact): agent spoke 10s, caller spoke 12s, silence 3s, 1 interruption(s)",
    )
    assert llm.calls == 1, "sentiment must not add a second LLM pass"
    assert outcome is not None and outcome.sentiment == "positive"
    # The talk-time context reached the prompt, labelled with its method.
    assert "SPEAKING TIME (exact)" in llm.last_user


# ── Persistence, retention, reporting ────────────────────────────────


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "pincer.db")


async def _seed_call(db_path: str, call_sid: str, started_at: datetime, direction: str = "inbound") -> None:
    from pincer.voice.retention import ensure_voice_tables

    async with aiosqlite.connect(db_path) as db:
        await ensure_voice_tables(db)
        await db.execute(
            "INSERT OR REPLACE INTO voice_calls (call_sid, direction, started_at, ended_at) VALUES (?, ?, ?, ?)",
            (call_sid, direction, started_at.isoformat(), (started_at + timedelta(minutes=1)).isoformat()),
        )
        await db.commit()


async def test_analytics_round_trip(db_path):
    record = CallAnalytics(
        agent_speech_ms=12_000,
        caller_speech_ms=8_000,
        silence_ms=5_000,
        overlap_ms=500,
        interruptions=2,
        talk_ratio=0.6,
        method=METHOD_EXACT,
        sentiment="mixed",
        sentiment_trajectory="improving",
        sentiment_rationale="Started annoyed about the wait, thanked the agent at the end.",
        created_at=datetime.now(UTC).isoformat(),
    )
    await an.save_analytics(db_path, "CA1", record)

    loaded = await an.load_analytics(db_path, "CA1")
    assert loaded is not None
    assert loaded.talk_ratio == 0.6
    assert loaded.method == METHOD_EXACT
    assert loaded.sentiment == "mixed"
    assert loaded.interruptions == 2

    assert (await an.load_many(db_path, ["CA1", "CA_missing"])).keys() == {"CA1"}
    assert await an.load_analytics(db_path, "CA_missing") is None


async def test_rationale_nulled_on_retention_purge(db_path):
    """The numbers are derived facts and survive; the sentence that quotes the
    call goes when the transcript goes."""
    from pincer.voice.retention import purge_expired_voice_data

    old = datetime.now(UTC) - timedelta(days=200)
    await _seed_call(db_path, "CA_old", old)
    await an.save_analytics(
        db_path,
        "CA_old",
        CallAnalytics(
            agent_speech_ms=10_000,
            caller_speech_ms=10_000,
            talk_ratio=0.5,
            method=METHOD_EXACT,
            sentiment="negative",
            sentiment_rationale="Said the third delay was unacceptable.",
            created_at=old.isoformat(),
        ),
    )

    await purge_expired_voice_data(db_path, retention_days=90)

    survivor = await an.load_analytics(db_path, "CA_old")
    assert survivor is not None, "the analytics row itself is a derived fact and survives"
    assert survivor.sentiment == "negative"
    assert survivor.talk_ratio == 0.5
    assert survivor.sentiment_rationale is None, "the quoted sentence must not outlive the transcript"


async def test_recent_rationale_is_not_purged(db_path):
    from pincer.voice.retention import purge_expired_voice_data

    now = datetime.now(UTC)
    await _seed_call(db_path, "CA_new", now)
    await an.save_analytics(
        db_path,
        "CA_new",
        CallAnalytics(
            sentiment="negative",
            sentiment_rationale="Complained about the wait.",
            method=METHOD_EXACT,
            created_at=now.isoformat(),
        ),
    )
    await purge_expired_voice_data(db_path, retention_days=90)
    survivor = await an.load_analytics(db_path, "CA_new")
    assert survivor is not None and survivor.sentiment_rationale == "Complained about the wait."


async def test_sentiment_distribution_counts_only_assessed(db_path):
    now = datetime.now(UTC)
    for sid, sentiment, direction in [
        ("CA_a", "positive", "inbound"),
        ("CA_b", "negative", "inbound"),
        ("CA_c", None, "inbound"),  # too short — not a neutral call
        ("CA_d", "positive", "outbound"),
    ]:
        await _seed_call(db_path, sid, now, direction)
        await an.save_analytics(db_path, sid, CallAnalytics(sentiment=sentiment, method=METHOD_EXACT))

    inbound = await an.sentiment_distribution(db_path, days=7, direction="inbound")
    assert inbound == {"positive": 1, "neutral": 0, "negative": 1, "mixed": 0}

    everything = await an.sentiment_distribution(db_path, days=7)
    assert everything["positive"] == 2


async def test_count_negative_since(db_path):
    now = datetime.now(UTC)
    for i in range(3):
        await _seed_call(db_path, f"CA_n{i}", now)
        await an.save_analytics(db_path, f"CA_n{i}", CallAnalytics(sentiment="negative", method=METHOD_EXACT))
    await _seed_call(db_path, "CA_old", now - timedelta(days=5))
    await an.save_analytics(db_path, "CA_old", CallAnalytics(sentiment="negative", method=METHOD_EXACT))

    cutoff = (now - timedelta(hours=24)).isoformat()
    assert await an.count_negative_since(db_path, cutoff) == 3


def test_negative_only_report_line():
    """Only a negative reading earns a line — the rest stay silent."""
    negative = CallAnalytics(sentiment="negative", sentiment_rationale="Said the third delay was unacceptable.")
    assert an.render_report_line(negative, "en") == (
        "⚠️ The caller seemed dissatisfied: Said the third delay was unacceptable."
    )
    assert "wirkte unzufrieden" in an.render_report_line(negative, "de")

    for sentiment in ("positive", "neutral", "mixed", None):
        assert an.render_report_line(CallAnalytics(sentiment=sentiment), "en") == ""
    assert an.render_report_line(None, "en") == ""


def test_report_line_hedges_rather_than_verdicts():
    """Copy rule: 'seemed', never 'is'."""
    line = an.render_report_line(CallAnalytics(sentiment="negative", sentiment_rationale="x"), "en")
    assert "seemed" in line
    assert " is " not in line
    assert "wirkte" in an.render_report_line(CallAnalytics(sentiment="negative", sentiment_rationale="x"), "de")


def test_report_line_survives_a_missing_rationale():
    line = an.render_report_line(CallAnalytics(sentiment="negative"), "en")
    assert line and "seemed dissatisfied" in line


# ── End to end through the post-call pipeline ────────────────────────


class _PipelineLLM:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls = 0
        self.prompts: list[str] = []

    async def complete(self, messages, tools=None, model=None, max_tokens=None, temperature=None, system=None):
        self.calls += 1
        self.prompts.append(messages[0].content)
        return SimpleNamespace(content=self.payload)


def _outcome_payload(**overrides) -> str:
    data = {
        "outcome": "completed",
        "task_result": "The delivery date was confirmed.",
        "key_facts": [],
        "commitments": [],
        "follow_up_suggestions": [],
        "language": "en",
        "sentiment": "negative",
        "sentiment_trajectory": "declining",
        "sentiment_rationale": "Said the third delay was unacceptable.",
    }
    data.update(overrides)
    return json.dumps(data)


async def _run_pipeline(tmp_path, payload: str, *, conversed: bool = True, speech_s: float = 20.0):
    from pincer.voice import status_notify
    from pincer.voice.engine import CallDirection, CallState
    from pincer.voice.postcall import PostCallProcessor
    from pincer.voice.transcript import Speaker, TranscriptLogger

    settings = SimpleNamespace(
        db_path=str(tmp_path / "pincer.db"),
        voice_auto_followup=False,
        dashboard_url="",
        default_user_id="tester",
        thread_inbound_context="off",
        thread_match_window_days=7,
        thread_autoclose_days=30,
    )
    sid = "CA_pipeline"
    state = CallState(
        call_sid=sid,
        direction=CallDirection.OUTBOUND,
        caller_number="+4915212345678",
        target_number="+4930111222",
        target_name="Praxis",
        purpose="Confirm the delivery date for the order.",
        language="en",
    )
    if not conversed:
        state.metadata["failure_code"] = "voicemail"
    state.ended_at = state.started_at + timedelta(seconds=60)

    clock = FakeClock()
    acc = TalkTimeAccumulator(method=METHOD_EXACT, clock=clock)
    acc.agent_audio_begin()
    acc.agent_audio_bytes(int(ONE_SECOND * speech_s))
    clock.advance(speech_s)
    acc.caller_span(speech_s, speech_s + speech_s)
    acc.interruption()
    state.metadata["talktime"] = acc

    transcript = TranscriptLogger(sid)
    transcript.log_utterance(Speaker.AGENT, "Calling about the delivery date.")
    transcript.log_utterance(Speaker.CALLER, "Said the third delay was unacceptable.")

    status_notify._reset_for_tests()
    sent: list[str] = []

    async def notifier(user_id, channel, text):
        sent.append(text)
        return True

    status_notify.set_status_notifier(notifier)
    status_notify.register_outbound_call(sid, user_id="tester", channel="telegram", language="en")

    llm = _PipelineLLM(payload)
    processor = PostCallProcessor(settings, llm=llm, memory=None, db_path=settings.db_path)
    report = await processor.process(sid, state, transcript, completed=conversed)
    status_notify._reset_for_tests()
    return SimpleNamespace(report=report, sent=sent, llm=llm, db_path=settings.db_path, call_sid=sid)


async def test_pipeline_persists_analytics_and_adds_only_the_negative_line(tmp_path):
    run = await _run_pipeline(tmp_path, _outcome_payload())

    assert run.llm.calls == 1, "analytics must not add an LLM call to the pipeline"
    assert "SPEAKING TIME (exact)" in run.llm.prompts[0]

    record = await an.load_analytics(run.db_path, run.call_sid)
    assert record is not None
    assert record.method == METHOD_EXACT
    assert record.agent_speech_ms == 20_000
    assert record.caller_speech_ms == 20_000
    assert record.interruptions == 1
    assert record.talk_ratio == pytest.approx(0.5, abs=0.01)
    assert record.sentiment == "negative"

    assert "The caller seemed dissatisfied: Said the third delay was unacceptable." in run.report
    assert run.sent == [run.report]


async def test_pipeline_stays_silent_on_a_positive_call(tmp_path):
    run = await _run_pipeline(
        tmp_path,
        _outcome_payload(sentiment="positive", sentiment_rationale="Thanked the agent twice."),
    )
    assert "seemed dissatisfied" not in run.report
    record = await an.load_analytics(run.db_path, run.call_sid)
    assert record is not None and record.sentiment == "positive"


async def test_pipeline_writes_a_null_row_for_voicemail(tmp_path):
    run = await _run_pipeline(tmp_path, _outcome_payload(outcome="voicemail"), conversed=False)
    record = await an.load_analytics(run.db_path, run.call_sid)
    assert record is not None, "a voicemail still gets a row saying there was nothing to measure"
    assert record.agent_speech_ms is None
    assert record.sentiment is None
    assert record.sentiment_reason == REASON_NOT_CONVERSED
    assert "seemed dissatisfied" not in run.report


async def test_pipeline_marks_a_short_call_unassessed(tmp_path):
    run = await _run_pipeline(tmp_path, _outcome_payload(), speech_s=2.0)
    record = await an.load_analytics(run.db_path, run.call_sid)
    assert record is not None
    assert record.sentiment is None
    assert record.sentiment_reason == REASON_TOO_SHORT
    assert record.agent_speech_ms == 2000, "talk time is still measured; only the reading is withheld"
    assert "seemed dissatisfied" not in run.report


async def test_pipeline_survives_a_failed_extraction(tmp_path):
    run = await _run_pipeline(tmp_path, "not json at all")
    record = await an.load_analytics(run.db_path, run.call_sid)
    assert record is not None
    assert record.sentiment is None
    assert record.sentiment_reason == REASON_EXTRACTION_FAILED
    assert record.talk_ratio is not None, "the measurement half does not depend on the LLM"
    assert run.report, "the Sprint 3 fallback report is still delivered"


# ── The negative-sentiment alert ─────────────────────────────────────


async def test_negative_sentiment_alert_fires_at_three(tmp_path):
    from pincer.observability.alerts import Severity, evaluate
    from pincer.observability.golden_signals import collect

    settings = SimpleNamespace(db_path=str(tmp_path / "pincer.db"))
    now = datetime.now(UTC)
    for i in range(2):
        await _seed_call(settings.db_path, f"CA_x{i}", now)
        await an.save_analytics(settings.db_path, f"CA_x{i}", CallAnalytics(sentiment="negative", method=METHOD_EXACT))

    signals = await collect(settings)
    assert signals.negative_sentiment is not None
    assert signals.negative_sentiment.value == 2
    assert not [a for a in evaluate(signals, settings) if a.rule == "negative_sentiment"]

    await _seed_call(settings.db_path, "CA_x2", now)
    await an.save_analytics(settings.db_path, "CA_x2", CallAnalytics(sentiment="negative", method=METHOD_EXACT))

    signals = await collect(settings)
    fired = [a for a in evaluate(signals, settings) if a.rule == "negative_sentiment"]
    assert len(fired) == 1
    assert fired[0].severity is Severity.NOTIFY, "unhappy callers notify; they do not page"
    assert "3 call(s)" in fired[0].title
