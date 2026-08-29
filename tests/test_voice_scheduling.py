"""Unit tests for voice/scheduling.py — slot computation, timeframes,
free/busy parsing, and the appointment confirmation token."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from pincer.voice import scheduling
from pincer.voice.engine import CallDirection, CallState
from pincer.voice.scheduling import (
    AppointmentTask,
    build_call_context,
    compute_candidate_slots,
    describe_slot,
    parse_business_days,
    parse_business_hours,
    parse_freebusy_output,
    process_appointment_response,
    resolve_timeframe,
)
from pincer.voice.transcript import TranscriptLogger

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 8, 19, 12, 0, tzinfo=BERLIN)  # Wednesday

BUSINESS = (time(9, 0), time(17, 0))
WEEKDAYS = {0, 1, 2, 3, 4}


def _settings(**overrides):
    values = {
        "voice_timezone": "Europe/Berlin",
        "timezone": "",
        "voice_default_language": "en",
        "voice_supported_languages": "en,de,uk",
        "voice_de_formality": "sie",
        "business_hours": "09:00-17:00",
        "business_days": "mon,tue,wed,thu,fri",
        "slot_buffer_min": 15,
        "scheduling_calendar_id": "primary",
        "voice_retry_attempts": 2,
        "voice_retry_delay_min": 30,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _task(**overrides) -> AppointmentTask:
    values = dict(
        task_id="t1",
        user_id="u1",
        channel="telegram",
        target_number="+4930123456",
        contact_name="Dr. Müller",
        topic="Zahnreinigung",
        timeframe="next_week",
        duration_minutes=30,
        language="de",
        candidates=["2026-08-25T14:00:00+02:00", "2026-08-26T09:00:00+02:00"],
    )
    values.update(overrides)
    return AppointmentTask(**values)


@pytest.fixture(autouse=True)
def _clean_registry():
    scheduling._reset_for_tests()
    yield
    scheduling._reset_for_tests()


# ── Config parsing ───────────────────────────────────────────────────


class TestConfigParsing:
    def test_business_hours(self):
        assert parse_business_hours("08:30-18:00") == (time(8, 30), time(18, 0))

    @pytest.mark.parametrize("bad", ["", "banana", "17:00-09:00", "9-17", None])
    def test_business_hours_fallback(self, bad):
        assert parse_business_hours(bad) == (time(9, 0), time(17, 0))

    def test_business_days(self):
        assert parse_business_days("mon,wed,fri") == {0, 2, 4}
        assert parse_business_days("Monday, Tuesday") == {0, 1}  # 3-letter prefixes

    def test_business_days_fallback(self):
        assert parse_business_days("") == {0, 1, 2, 3, 4}
        assert parse_business_days("banana") == {0, 1, 2, 3, 4}


class TestResolveTimeframe:
    def test_tomorrow(self):
        start, end = resolve_timeframe("tomorrow", NOW)
        assert start == datetime(2026, 8, 20, 0, 0, tzinfo=BERLIN)
        assert end == datetime(2026, 8, 21, 0, 0, tzinfo=BERLIN)

    def test_next_week_is_monday_to_sunday(self):
        start, end = resolve_timeframe("next_week", NOW)
        assert start == datetime(2026, 8, 24, 0, 0, tzinfo=BERLIN)  # next Monday
        assert end - start == timedelta(days=7)

    def test_this_week(self):
        start, end = resolve_timeframe("this_week", NOW)
        assert start == NOW
        assert end == datetime(2026, 8, 24, 0, 0, tzinfo=BERLIN)

    def test_iso_range_inclusive_end_day(self):
        start, end = resolve_timeframe("2026-09-01/2026-09-03", NOW)
        assert start == datetime(2026, 9, 1, 0, 0, tzinfo=BERLIN)
        assert end == datetime(2026, 9, 4, 0, 0, tzinfo=BERLIN)  # through Sep 3

    def test_iso_range_in_the_past_clamps_to_now(self):
        start, _end = resolve_timeframe("2026-08-01/2026-08-30", NOW)
        assert start == NOW

    @pytest.mark.parametrize("garbage", ["", "someday", "2026-13-99/2026-01-01"])
    def test_fallback_next_days(self, garbage):
        start, end = resolve_timeframe(garbage, NOW)
        assert start == NOW
        assert end > NOW + timedelta(days=6)


# ── Free/busy parsing ────────────────────────────────────────────────


class TestParseFreebusy:
    def test_free(self):
        assert parse_freebusy_output("primary: FREE") == []

    def test_busy_intervals(self):
        text = (
            "primary: BUSY at:\n"
            "    2026-08-25T08:00:00Z → 2026-08-25T09:00:00Z\n"
            "    2026-08-25T12:00:00+02:00 → 2026-08-25T13:30:00+02:00"
        )
        busy = parse_freebusy_output(text)
        assert busy is not None and len(busy) == 2
        assert busy[0][0] == datetime(2026, 8, 25, 8, 0, tzinfo=ZoneInfo("UTC"))

    @pytest.mark.parametrize("garbage", ["", "Error: auth failed", "No free/busy data returned.", None])
    def test_unrecognized_is_none_never_free(self, garbage):
        assert parse_freebusy_output(garbage) is None


# ── Slot computation ─────────────────────────────────────────────────


def _slots(busy, start, end, **overrides):
    kwargs = dict(
        duration_minutes=30,
        business_hours=BUSINESS,
        business_days=WEEKDAYS,
        buffer_minutes=15,
        now=NOW,
    )
    kwargs.update(overrides)
    return compute_candidate_slots(busy, start, end, **kwargs)


class TestComputeCandidateSlots:
    def test_empty_calendar_earliest_first(self):
        start, end = resolve_timeframe("next_week", NOW)
        slots = _slots([], start, end)
        assert slots == [
            datetime(2026, 8, 24, 9, 0, tzinfo=BERLIN),
            datetime(2026, 8, 24, 9, 30, tzinfo=BERLIN),
            datetime(2026, 8, 24, 10, 0, tzinfo=BERLIN),
        ]

    def test_full_calendar_returns_empty(self):
        start, end = resolve_timeframe("tomorrow", NOW)
        busy = [(start, end)]
        assert _slots(busy, start, end) == []

    def test_buffer_respected(self):
        # Busy 10:00-11:00; with a 15-minute buffer the 09:30 slot (ends 10:00,
        # padded to 10:15) collides, the 09:00 slot (padded end 09:45) is fine.
        day = datetime(2026, 8, 24, 0, 0, tzinfo=BERLIN)
        busy = [(day.replace(hour=10), day.replace(hour=11))]
        slots = _slots(busy, day, day + timedelta(days=1))
        assert day.replace(hour=9) in slots
        assert day.replace(hour=9, minute=30) not in slots
        assert day.replace(hour=11) not in slots  # padded start 10:45 collides
        assert day.replace(hour=11, minute=30) in slots

    def test_business_hours_edges(self):
        # 60-minute appointment must END by 17:00 — last start is 16:00.
        day = datetime(2026, 8, 24, 0, 0, tzinfo=BERLIN)
        busy = [(day.replace(hour=9), day.replace(hour=16))]
        slots = _slots(busy, day, day + timedelta(days=1), duration_minutes=60, buffer_minutes=0)
        assert slots == [day.replace(hour=16)]

    def test_weekend_skipped(self):
        saturday = datetime(2026, 8, 22, 0, 0, tzinfo=BERLIN)
        slots = _slots([], saturday, saturday + timedelta(days=2))  # Sat + Sun
        assert slots == []

    def test_min_lead_excludes_immediate_slots(self):
        # now = Wednesday 12:00 — slots today start at 13:00 (60 min lead), on the grid
        day_start = NOW.replace(hour=0, minute=0)
        slots = _slots([], day_start, day_start + timedelta(days=1))
        assert slots[0] == NOW.replace(hour=13, minute=0)

    def test_dst_spring_forward_keeps_wall_clock(self):
        # Europe/Berlin DST starts Sun 2026-03-29; Monday 09:00 must be 09:00
        # local with the NEW +02:00 offset.
        now = datetime(2026, 3, 26, 12, 0, tzinfo=BERLIN)
        start = datetime(2026, 3, 29, 0, 0, tzinfo=BERLIN)
        slots = _slots([], start, start + timedelta(days=2), now=now)
        first = slots[0]
        assert (first.hour, first.minute) == (9, 0)
        assert first.utcoffset() == timedelta(hours=2)  # CEST after the switch
        assert first.date() == datetime(2026, 3, 30).date()  # Sunday skipped

    def test_utc_busy_blocks_local_slot_across_dst(self):
        # Busy 07:00-08:00 UTC on 2026-03-30 == 09:00-10:00 Berlin CEST
        now = datetime(2026, 3, 26, 12, 0, tzinfo=BERLIN)
        start = datetime(2026, 3, 30, 0, 0, tzinfo=BERLIN)
        busy = [
            (
                datetime(2026, 3, 30, 7, 0, tzinfo=ZoneInfo("UTC")),
                datetime(2026, 3, 30, 8, 0, tzinfo=ZoneInfo("UTC")),
            )
        ]
        slots = _slots(busy, start, start + timedelta(days=1), now=now, buffer_minutes=0)
        assert slots[0] == datetime(2026, 3, 30, 10, 0, tzinfo=BERLIN)


class TestDescribeSlot:
    def test_german(self):
        dt = datetime(2026, 8, 25, 14, 0, tzinfo=BERLIN)
        assert describe_slot(dt, "de") == "Dienstag, 25.08.2026 um 14:00 Uhr"

    def test_english(self):
        dt = datetime(2026, 8, 25, 14, 0, tzinfo=BERLIN)
        assert describe_slot(dt, "en") == "Tuesday, August 25, 2026 at 14:00"


# ── Call context ─────────────────────────────────────────────────────


class TestBuildCallContext:
    def test_context_contains_rules_and_candidates(self):
        task = _task()
        scheduling.register_appointment("CA1", task)
        context = build_call_context("CA1", _settings(), "de")
        assert "TERMIN-REGELN" in context
        assert "Dr. Müller" in context
        assert "Zahnreinigung" in context
        assert "2026-08-25T14:00:00+02:00" in context
        assert "Dienstag, 25.08.2026 um 14:00 Uhr" in context
        assert "[APPOINTMENT_CONFIRMED:" in context

    def test_empty_for_unknown_call(self):
        assert build_call_context("CA-nope", _settings(), "de") == ""

    def test_english_pack_for_english_call(self):
        scheduling.register_appointment("CA1", _task(language="en"))
        context = build_call_context("CA1", _settings(), "en")
        assert "APPOINTMENT RULES" in context


# ── Confirmation token ───────────────────────────────────────────────


def _state(call_sid="CA1", language="de") -> CallState:
    return CallState(
        call_sid=call_sid,
        direction=CallDirection.OUTBOUND,
        caller_number="+15550001111",
        target_number="+4930123456",
        language=language,
    )


class TestProcessAppointmentResponse:
    def test_valid_candidate_confirms_and_strips(self):
        task = _task()
        scheduling.register_appointment("CA1", task)
        transcript = TranscriptLogger("CA1")
        text = process_appointment_response(
            "[APPOINTMENT_CONFIRMED:2026-08-25T14:00:00+02:00] Wunderbar, dann bis Dienstag!",
            _state(),
            _settings(),
            transcript,
        )
        assert text == "Wunderbar, dann bis Dienstag!"
        assert task.status == "confirmed"
        assert task.agreed_start == "2026-08-25T14:00:00+02:00"
        actions = [a for a in transcript.actions if a.action_type == "appointment_confirmed"]
        assert len(actions) == 1 and actions[0].output_summary == "2026-08-25T14:00:00+02:00"

    def test_naive_token_resolved_in_voice_timezone(self):
        task = _task()
        scheduling.register_appointment("CA1", task)
        text = process_appointment_response(
            "[APPOINTMENT_CONFIRMED:2026-08-25T14:00:00] Passt.", _state(), _settings(), None
        )
        assert task.status == "confirmed"
        assert text == "Passt."

    def test_token_only_falls_back_to_ack(self):
        scheduling.register_appointment("CA1", _task())
        text = process_appointment_response(
            "[APPOINTMENT_CONFIRMED:2026-08-25T14:00:00+02:00]", _state(), _settings(), None
        )
        assert text  # localized ack, never empty speech
        assert "[APPOINTMENT_CONFIRMED" not in text

    def test_out_of_candidates_is_never_spoken(self):
        """The hard backstop: an out-of-list confirmation is replaced by the
        deferral line — the callee never hears the commitment."""
        task = _task()
        scheduling.register_appointment("CA1", task)
        transcript = TranscriptLogger("CA1")
        text = process_appointment_response(
            "[APPOINTMENT_CONFIRMED:2026-08-27T18:00:00+02:00] Abgemacht, Donnerstag 18 Uhr!",
            _state(),
            _settings(),
            transcript,
        )
        assert "Abgemacht" not in text
        assert "18 Uhr" not in text
        assert "abstimmen" in text or "melde mich" in text  # de deferral line
        assert task.status == "out_of_candidates"
        assert task.proposed_out_of_slot == "2026-08-27T18:00:00+02:00"
        assert any(a.action_type == "appointment_deferred" for a in transcript.actions)

    def test_unparseable_token_defers(self):
        task = _task()
        scheduling.register_appointment("CA1", task)
        text = process_appointment_response("[APPOINTMENT_CONFIRMED:14.00] Ok!", _state(), _settings(), None)
        assert task.status == "out_of_candidates"
        assert "Ok!" not in text

    def test_no_task_untouched(self):
        text = process_appointment_response(
            "[APPOINTMENT_CONFIRMED:2026-08-25T14:00:00+02:00] Hi", _state("CA-none"), _settings(), None
        )
        assert text.startswith("[APPOINTMENT_CONFIRMED")

    def test_no_token_untouched(self):
        scheduling.register_appointment("CA1", _task())
        assert process_appointment_response("Gerne!", _state(), _settings(), None) == "Gerne!"
