"""Tests for German comprehension helpers (Sprint 2, T2.4)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from pincer.voice.nlu_de import (
    number_words_de,
    ordinal_words_de,
    parse_relative_datetime_de,
    render_date_de,
    render_datetime_de,
    render_time_de,
)
from pincer.voice.safety_gates import ConfirmationStatus, parse_confirmation

TZ = ZoneInfo("Europe/Berlin")

# Friday, 2025-08-15 10:00 CEST — a fixed anchor for relative parsing
NOW = datetime(2025, 8, 15, 10, 0, tzinfo=TZ)


class TestRelativeDates:
    def test_morgen(self):
        parsed = parse_relative_datetime_de("morgen um 10 Uhr", now=NOW)
        assert parsed and parsed.dt.date() == datetime(2025, 8, 16).date()

    def test_uebermorgen(self):
        parsed = parse_relative_datetime_de("übermorgen", now=NOW)
        assert parsed and parsed.dt.date() == datetime(2025, 8, 17).date()
        assert parsed.has_date and not parsed.has_time

    def test_morgens_is_not_tomorrow(self):
        # "um acht Uhr morgens" — 'morgens' must not be read as tomorrow
        parsed = parse_relative_datetime_de("um acht Uhr morgens", now=NOW)
        assert parsed is not None
        assert not parsed.has_date
        assert parsed.dt.hour == 8

    def test_naechste_woche_dienstag(self):
        # NOW is Friday 2025-08-15 (ISO week Mon 08-11). Next week's Tuesday = 08-19.
        parsed = parse_relative_datetime_de("nächste Woche Dienstag", now=NOW)
        assert parsed and parsed.dt.date() == datetime(2025, 8, 19).date()

    def test_dienstag_naechste_woche(self):
        parsed = parse_relative_datetime_de("Dienstag nächste Woche", now=NOW)
        assert parsed and parsed.dt.date() == datetime(2025, 8, 19).date()

    def test_bare_weekday_is_next_occurrence(self):
        # Friday asking for "Dienstag" → the coming Tuesday (08-19)
        parsed = parse_relative_datetime_de("am Dienstag", now=NOW)
        assert parsed and parsed.dt.date() == datetime(2025, 8, 19).date()

    def test_same_weekday_never_today(self):
        # Friday asking for "Freitag" → next Friday, not today
        parsed = parse_relative_datetime_de("am Freitag", now=NOW)
        assert parsed and parsed.dt.date() == datetime(2025, 8, 22).date()

    def test_in_acht_tagen_is_one_week(self):
        # German idiom: "in acht Tagen" = in a week
        parsed = parse_relative_datetime_de("in acht Tagen", now=NOW)
        assert parsed and parsed.dt.date() == datetime(2025, 8, 22).date()

    def test_in_drei_tagen_is_literal(self):
        parsed = parse_relative_datetime_de("in drei Tagen", now=NOW)
        assert parsed and parsed.dt.date() == datetime(2025, 8, 18).date()

    def test_in_zwei_wochen(self):
        parsed = parse_relative_datetime_de("in zwei Wochen", now=NOW)
        assert parsed and parsed.dt.date() == datetime(2025, 8, 29).date()

    def test_heute(self):
        parsed = parse_relative_datetime_de("heute um 16 Uhr", now=NOW)
        assert parsed and parsed.dt.date() == NOW.date()
        assert parsed.dt.hour == 16


class TestTimes:
    def test_halb_drei_is_1430(self):
        parsed = parse_relative_datetime_de("um halb drei", now=NOW)
        assert parsed is not None
        assert (parsed.dt.hour, parsed.dt.minute) == (14, 30)

    def test_halb_drei_morgens(self):
        parsed = parse_relative_datetime_de("um halb drei morgens", now=NOW)
        assert parsed is not None
        assert (parsed.dt.hour, parsed.dt.minute) == (2, 30)

    def test_viertel_nach_vier(self):
        parsed = parse_relative_datetime_de("Viertel nach vier", now=NOW)
        assert parsed is not None
        assert (parsed.dt.hour, parsed.dt.minute) == (16, 15)

    def test_viertel_vor_fuenf(self):
        parsed = parse_relative_datetime_de("Viertel vor fünf", now=NOW)
        assert parsed is not None
        assert (parsed.dt.hour, parsed.dt.minute) == (16, 45)

    def test_dreiviertel_vier_is_1545(self):
        # Southern German: "dreiviertel vier" = 15:45
        parsed = parse_relative_datetime_de("dreiviertel vier", now=NOW)
        assert parsed is not None
        assert (parsed.dt.hour, parsed.dt.minute) == (15, 45)

    def test_um_15_uhr(self):
        parsed = parse_relative_datetime_de("um 15 Uhr", now=NOW)
        assert parsed is not None
        assert (parsed.dt.hour, parsed.dt.minute) == (15, 0)

    def test_um_14_30(self):
        parsed = parse_relative_datetime_de("um 14:30", now=NOW)
        assert parsed is not None
        assert (parsed.dt.hour, parsed.dt.minute) == (14, 30)

    def test_um_14_uhr_30(self):
        parsed = parse_relative_datetime_de("um 14 Uhr 30", now=NOW)
        assert parsed is not None
        assert (parsed.dt.hour, parsed.dt.minute) == (14, 30)

    def test_bare_um_drei_business_hours(self):
        parsed = parse_relative_datetime_de("um drei", now=NOW)
        assert parsed is not None
        assert (parsed.dt.hour, parsed.dt.minute) == (15, 0)

    def test_abends(self):
        parsed = parse_relative_datetime_de("um acht Uhr abends", now=NOW)
        assert parsed is not None
        assert parsed.dt.hour == 20

    def test_time_only_past_rolls_to_tomorrow(self):
        # NOW is 10:00; "um neun" (→ 9:00 is past, heuristic keeps 9? 9 not in 1..7 range)
        parsed = parse_relative_datetime_de("um neun", now=NOW)
        assert parsed is not None
        assert parsed.dt.hour == 9
        assert parsed.dt.date() == datetime(2025, 8, 16).date()  # next occurrence


class TestCombined:
    def test_acceptance_phrase(self):
        # "um halb drei nächste Woche Dienstag" → Tuesday 2025-08-19, 14:30
        parsed = parse_relative_datetime_de("um halb drei nächste Woche Dienstag", now=NOW)
        assert parsed is not None
        assert parsed.dt == datetime(2025, 8, 19, 14, 30, tzinfo=TZ)
        assert parsed.has_date and parsed.has_time
        # ...and echoes back correctly in German
        assert render_datetime_de(parsed.dt) == "Dienstag, der neunzehnte August um vierzehn Uhr dreißig"

    def test_morgen_um_halb_drei(self):
        parsed = parse_relative_datetime_de("morgen um halb drei", now=NOW)
        assert parsed is not None
        assert parsed.dt == datetime(2025, 8, 16, 14, 30, tzinfo=TZ)

    def test_no_match_returns_none(self):
        assert parse_relative_datetime_de("das Wetter ist schön", now=NOW) is None
        assert parse_relative_datetime_de("", now=NOW) is None


class TestDstBoundary:
    def test_across_spring_forward(self):
        # Saturday 2025-03-29; "morgen um halb drei" = Sunday 2025-03-30 14:30 CEST.
        # 02:30 does not exist that day; 14:30 must carry the +02:00 offset.
        now = datetime(2025, 3, 29, 10, 0, tzinfo=TZ)
        parsed = parse_relative_datetime_de("morgen um halb drei", now=now)
        assert parsed is not None
        assert parsed.dt == datetime(2025, 3, 30, 14, 30, tzinfo=TZ)
        assert parsed.dt.utcoffset().total_seconds() == 2 * 3600

    def test_across_fall_back(self):
        now = datetime(2025, 10, 25, 10, 0, tzinfo=TZ)
        parsed = parse_relative_datetime_de("morgen um 15 Uhr", now=now)
        assert parsed is not None
        assert parsed.dt == datetime(2025, 10, 26, 15, 0, tzinfo=TZ)
        assert parsed.dt.utcoffset().total_seconds() == 1 * 3600  # CET after fall-back


class TestRendering:
    def test_render_time(self):
        assert render_time_de(datetime(2025, 8, 19, 14, 30)) == "vierzehn Uhr dreißig"
        assert render_time_de(datetime(2025, 8, 19, 9, 0)) == "neun Uhr"
        assert render_time_de(datetime(2025, 8, 19, 1, 5)) == "ein Uhr fünf"

    def test_render_date(self):
        assert render_date_de(datetime(2025, 8, 18)) == "Montag, der achtzehnte August"
        assert render_date_de(datetime(2025, 8, 1)) == "Freitag, der erste August"
        assert render_date_de(datetime(2025, 8, 3)) == "Sonntag, der dritte August"
        assert render_date_de(datetime(2025, 12, 31)) == "Mittwoch, der einunddreißigste Dezember"

    def test_render_datetime_no_iso_leakage(self):
        rendered = render_datetime_de(datetime(2025, 8, 19, 14, 30))
        assert not any(ch.isdigit() for ch in rendered), rendered

    def test_number_words(self):
        assert number_words_de(0) == "null"
        assert number_words_de(21) == "einundzwanzig"
        assert number_words_de(30) == "dreißig"
        assert number_words_de(45) == "fünfundvierzig"
        assert number_words_de(59) == "neunundfünfzig"

    def test_ordinals(self):
        assert ordinal_words_de(1) == "erste"
        assert ordinal_words_de(3) == "dritte"
        assert ordinal_words_de(7) == "siebte"
        assert ordinal_words_de(19) == "neunzehnte"
        assert ordinal_words_de(20) == "zwanzigste"


class TestGermanConfirmation:
    @pytest.mark.parametrize(
        "utterance",
        ["Ja", "Ja, genau", "Passt", "Einverstanden", "In Ordnung", "Das stimmt", "Jawohl, machen Sie das", "Korrekt"],
    )
    def test_affirmatives(self, utterance):
        assert parse_confirmation(utterance) == ConfirmationStatus.CONFIRMED

    @pytest.mark.parametrize(
        "utterance",
        [
            "Nein",
            "Nee",
            "Lieber nicht",
            "Das passt nicht",
            "Auf keinen Fall",
            "Stimmt nicht",
            "Nicht richtig",
            "Falsch",
        ],
    )
    def test_negatives(self, utterance):
        assert parse_confirmation(utterance) == ConfirmationStatus.REJECTED

    def test_unclear_reasks(self):
        assert parse_confirmation("Hmm, vielleicht") == ConfirmationStatus.UNCLEAR
        assert parse_confirmation("") == ConfirmationStatus.UNCLEAR

    def test_english_still_works(self):
        assert parse_confirmation("Yes, go ahead") == ConfirmationStatus.CONFIRMED
        assert parse_confirmation("No, stop") == ConfirmationStatus.REJECTED
