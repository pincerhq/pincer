"""Tests for voice-facing timezone rendering (Sprint 0, DACH)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from pincer.voice.localtime import (
    format_voice_time,
    get_voice_timezone,
    to_voice_local,
    voice_today_str,
    with_voice_zone,
)


def _settings(voice_timezone="", timezone="Europe/Berlin"):
    return SimpleNamespace(voice_timezone=voice_timezone, timezone=timezone)


class TestGetVoiceTimezone:
    def test_voice_timezone_wins(self):
        tz = get_voice_timezone(_settings(voice_timezone="Europe/Vienna"))
        assert tz.key == "Europe/Vienna"

    def test_falls_back_to_settings_timezone(self):
        tz = get_voice_timezone(_settings(timezone="Europe/Zurich"))
        assert tz.key == "Europe/Zurich"

    def test_invalid_names_fall_back_to_berlin(self):
        tz = get_voice_timezone(_settings(voice_timezone="Not/AZone", timezone="Also/Bad"))
        assert tz.key == "Europe/Berlin"

    def test_empty_everything_defaults_to_berlin(self):
        tz = get_voice_timezone(_settings(timezone=""))
        assert tz.key == "Europe/Berlin"


class TestDstBoundaries:
    """Europe/Berlin switches CET(+1) -> CEST(+2) on the last Sunday of March
    (2025-03-30 02:00) and back on the last Sunday of October (2025-10-26 03:00)."""

    def test_spring_forward(self):
        settings = _settings()
        before = to_voice_local(datetime(2025, 3, 30, 0, 30, tzinfo=UTC), settings)
        after = to_voice_local(datetime(2025, 3, 30, 1, 30, tzinfo=UTC), settings)
        assert before.strftime("%H:%M") == "01:30"  # CET, UTC+1
        assert after.strftime("%H:%M") == "03:30"  # CEST, UTC+2 (02:30 does not exist)

    def test_fall_back(self):
        settings = _settings()
        before = to_voice_local(datetime(2025, 10, 26, 0, 30, tzinfo=UTC), settings)
        after = to_voice_local(datetime(2025, 10, 26, 1, 30, tzinfo=UTC), settings)
        assert before.strftime("%H:%M") == "02:30"  # CEST, UTC+2
        assert after.strftime("%H:%M") == "02:30"  # CET, UTC+1 (02:30 occurs twice)
        assert before.utcoffset() != after.utcoffset()

    def test_naive_input_is_utc(self):
        local = to_voice_local(datetime(2025, 6, 1, 12, 0), _settings())
        assert local.strftime("%H:%M") == "14:00"  # CEST


class TestFormatVoiceTime:
    def test_english(self):
        text = format_voice_time(datetime(2025, 3, 30, 1, 30, tzinfo=UTC), _settings(), language="en")
        assert text == "03:30 on Sunday, March 30"

    def test_german(self):
        text = format_voice_time(datetime(2025, 3, 30, 1, 30, tzinfo=UTC), _settings(), language="de")
        assert text == "03:30 Uhr am 30.03.2025"


class TestCalendarEventRendering:
    def test_agenda_converts_to_local_zone(self):
        from pincer.integrations.google.calendar_render import render_agenda

        event = {
            "summary": "Zahnarzt",
            "start": {"dateTime": "2025-03-30T01:30:00+00:00"},
            "end": {"dateTime": "2025-03-30T02:00:00+00:00"},
        }
        text = render_agenda([event], ZoneInfo("Europe/Berlin"), date(2025, 3, 30))
        # 01:30 UTC is 03:30 CEST after the spring-forward transition
        assert "03:30–04:00  Zahnarzt" in text

    def test_agenda_all_day_unchanged(self):
        from pincer.integrations.google.calendar_render import render_agenda

        event = {"summary": "Feiertag", "start": {"date": "2025-10-03"}, "end": {"date": "2025-10-04"}}
        text = render_agenda([event], ZoneInfo("Europe/Berlin"), date(2025, 10, 3))
        assert "all day  Feiertag" in text


def test_voice_today_str_uses_voice_zone():
    # Pick a zone far from UTC so the local date can differ from the UTC date
    settings = _settings(voice_timezone="Pacific/Kiritimati")  # UTC+14
    today = voice_today_str(settings)
    expected = datetime.now(ZoneInfo("Pacific/Kiritimati")).strftime("%Y-%m-%d")
    assert today == expected


def test_call_read_bounds_take_the_voice_zone_offset_and_leave_the_rest():
    """Date-time bounds get the voice zone's offset (DST-correct); a bare date, an offset-bearing bound
    and other arguments are passed through for the tool to read as before."""
    args = {
        "time_min": "2026-07-01T14:00:00",
        "time_max": "2026-11-12",
        "calendar_id": "primary",
    }
    assert with_voice_zone("google__list_events", args, _settings(voice_timezone="Europe/Berlin")) == {
        "time_min": "2026-07-01T14:00:00+02:00",
        "time_max": "2026-11-12",
        "calendar_id": "primary",
    }
    fixed = {"time_min": "2026-11-12T14:00:00+05:00", "time_max": "2026-11-12T15:00:00Z", "emails": "a@b.c"}
    assert with_voice_zone("google__check_freebusy", fixed, _settings()) is fixed
