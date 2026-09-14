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

    # Minutes spoken as words must not be dropped. A dropped minute is worse
    # than a parse failure: "um neun Uhr fünfundvierzig" became 09:00 and was
    # then read back as a clean time in VERIFY, so nothing flagged it.
    @pytest.mark.parametrize(
        ("utterance", "expected"),
        [
            ("um vierzehn Uhr dreißig", (14, 30)),
            ("um zehn Uhr fünfzehn", (10, 15)),
            ("um neun Uhr fünfundvierzig", (9, 45)),
            ("um acht Uhr fünf", (8, 5)),
            ("um elf Uhr einundzwanzig", (11, 21)),
            ("um vierzehn Uhr null", (14, 0)),
            # "fünfzehn"/"fünfundvierzig" must win over the "fünf" prefix.
            ("um zwölf Uhr fünfzig", (12, 50)),
        ],
    )
    def test_word_form_minutes(self, utterance, expected):
        parsed = parse_relative_datetime_de(utterance, now=NOW)
        assert parsed is not None, f"{utterance!r} did not parse"
        assert (parsed.dt.hour, parsed.dt.minute) == expected

    def test_word_and_digit_minutes_agree(self):
        words = parse_relative_datetime_de("um drei Uhr dreißig", now=NOW)
        digits = parse_relative_datetime_de("um 3 Uhr 30", now=NOW)
        assert words is not None and digits is not None
        assert (words.dt.hour, words.dt.minute) == (digits.dt.hour, digits.dt.minute) == (15, 30)

    # Naming a minute says nothing about which half of the day was meant, but
    # the branch used to skip the heuristic whenever a minute was captured —
    # so a 1–7 hour stayed literal AM and an explicit "nachmittags"/"abends"
    # was discarded outright.
    @pytest.mark.parametrize(
        ("utterance", "expected"),
        [
            # Business-hours default now applies with a minute present.
            ("um drei Uhr 15", (15, 15)),
            ("um drei Uhr fünfzehn", (15, 15)),
            ("um 3 Uhr 30", (15, 30)),
            ("um vier Uhr zwanzig", (16, 20)),
            ("um sieben Uhr dreißig", (19, 30)),
            # 8–12 are already unambiguous business hours and stay AM.
            ("um acht Uhr fünf", (8, 5)),
            ("um zehn Uhr fünfzehn", (10, 15)),
            ("um zwölf Uhr fünfzig", (12, 50)),
            # A 24-hour hour is never touched.
            ("um vierzehn Uhr dreißig", (14, 30)),
            ("um 14 Uhr 30", (14, 30)),
            ("um 20 Uhr 15", (20, 15)),
            # Explicit markers are honoured again, minute or not.
            ("um drei Uhr dreißig morgens", (3, 30)),
            ("um drei Uhr dreißig nachts", (3, 30)),
            ("um drei Uhr dreißig nachmittags", (15, 30)),
            ("um acht Uhr fünfzehn abends", (20, 15)),
            ("um zwölf Uhr dreißig nachts", (0, 30)),
        ],
    )
    def test_daytime_heuristic_applies_with_minutes(self, utterance, expected):
        parsed = parse_relative_datetime_de(utterance, now=NOW)
        assert parsed is not None, f"{utterance!r} did not parse"
        assert (parsed.dt.hour, parsed.dt.minute) == expected

    @pytest.mark.parametrize(
        ("explicit", "idiomatic"),
        [
            ("um drei Uhr dreißig", "um halb vier"),
            ("um drei Uhr fünfzehn", "um viertel nach drei"),
            ("um drei Uhr fünfundvierzig", "um viertel vor vier"),
        ],
    )
    def test_explicit_minute_matches_idiomatic_form(self, explicit, idiomatic):
        """The same clock time said two ways must resolve identically."""
        a = parse_relative_datetime_de(explicit, now=NOW)
        b = parse_relative_datetime_de(idiomatic, now=NOW)
        assert a is not None and b is not None
        assert (a.dt.hour, a.dt.minute) == (b.dt.hour, b.dt.minute)

    def test_every_rendered_minute_parses_back(self):
        """The parser must read back any time this module speaks.

        `render_time_de` uses `number_words_de` for the minute, so the minute
        vocabulary is derived from it rather than hand-listed — this guards the
        two halves against drifting apart again.
        """
        for minute in range(60):
            spoken = "um " + render_time_de(datetime(2025, 8, 15, 14, minute, tzinfo=TZ))
            parsed = parse_relative_datetime_de(spoken, now=NOW)
            assert parsed is not None, f"{spoken!r} did not parse"
            assert (parsed.dt.hour, parsed.dt.minute) == (14, minute), spoken

    @pytest.mark.parametrize("utterance", ["um 14 Uhr 75", "um 14 Uhr 99"])
    def test_impossible_minute_is_not_accepted(self, utterance):
        # Better to fall back to the LLM than to invent a time.
        parsed = parse_relative_datetime_de(utterance, now=NOW)
        assert parsed is None or parsed.dt.minute <= 59

    # "halb eins"/"viertel vor eins" subtract 1 from the target hour, so they
    # land on 0. That 0 is the 12-hour clock's twelve o'clock, not midnight:
    # a caller proposing a lunchtime slot was booked at 00:30 — and since that
    # is already past, on the following day.
    @pytest.mark.parametrize(
        ("utterance", "expected"),
        [
            ("halb eins", (12, 30)),
            ("um halb eins", (12, 30)),
            ("um viertel vor eins", (12, 45)),
            ("um dreiviertel eins", (12, 45)),
            ("um halb eins mittags", (12, 30)),
            ("um halb eins nachmittags", (12, 30)),
        ],
    )
    def test_halb_eins_is_lunchtime(self, utterance, expected):
        parsed = parse_relative_datetime_de(utterance, now=NOW)
        assert parsed is not None, f"{utterance!r} did not parse"
        assert (parsed.dt.hour, parsed.dt.minute) == expected

    def test_halb_eins_stays_today(self):
        # NOW is 10:00 on the 15th; 12:30 is still ahead, so it must not roll.
        parsed = parse_relative_datetime_de("um halb eins", now=NOW)
        assert parsed is not None
        assert parsed.dt.date() == datetime(2025, 8, 15).date()

    @pytest.mark.parametrize(
        ("utterance", "expected"),
        [
            ("um halb eins nachts", (0, 30)),
            ("um halb eins in der Nacht", (0, 30)),
            ("um halb eins morgens", (0, 30)),
            ("um halb eins früh", (0, 30)),
            ("um viertel vor eins nachts", (0, 45)),
            ("um dreiviertel eins nachts", (0, 45)),
        ],
    )
    def test_halb_eins_with_night_marker_is_midnight(self, utterance, expected):
        parsed = parse_relative_datetime_de(utterance, now=NOW)
        assert parsed is not None, f"{utterance!r} did not parse"
        assert (parsed.dt.hour, parsed.dt.minute) == expected

    @pytest.mark.parametrize(
        ("utterance", "expected"),
        [("um 0 Uhr", (0, 0)), ("um 0 Uhr 30", (0, 30)), ("um 00 Uhr 15", (0, 15))],
    )
    def test_explicit_zero_hour_is_not_wrapped_to_noon(self, utterance, expected):
        """An explicit 24-hour "0 Uhr" really is midnight — only the half-to
        arithmetic produces a 0 that means twelve o'clock."""
        parsed = parse_relative_datetime_de(utterance, now=NOW)
        assert parsed is not None, f"{utterance!r} did not parse"
        assert (parsed.dt.hour, parsed.dt.minute) == expected

    def test_bare_um_drei_business_hours(self):
        parsed = parse_relative_datetime_de("um drei", now=NOW)
        assert parsed is not None
        assert (parsed.dt.hour, parsed.dt.minute) == (15, 0)

    def test_abends(self):
        parsed = parse_relative_datetime_de("um acht Uhr abends", now=NOW)
        assert parsed is not None
        assert parsed.dt.hour == 20

    # "nachts" is not "abends": the small hours must stay AM. Booking the
    # opposite half of the day survives VERIFY, because the wrong time is read
    # back as if it were what the caller said.
    @pytest.mark.parametrize(
        ("utterance", "expected"),
        [
            ("um ein Uhr nachts", (1, 0)),
            ("um zwei Uhr nachts", (2, 0)),
            ("um drei Uhr nachts", (3, 0)),
            ("um fünf Uhr nachts", (5, 0)),
            ("um acht Uhr nachts", (8, 0)),
            # Late evening end of the night still means PM.
            ("um neun Uhr nachts", (21, 0)),
            ("um zehn Uhr nachts", (22, 0)),
            ("um elf Uhr nachts", (23, 0)),
            # "zwölf Uhr nachts" is midnight, not noon.
            ("um zwölf Uhr nachts", (0, 0)),
            # Half-hour forms feed hour-1 into the same heuristic.
            ("um halb eins nachts", (0, 30)),
            ("um halb drei nachts", (2, 30)),
            # 24h input is already unambiguous and must pass through.
            ("um 23 Uhr nachts", (23, 0)),
            # "in der Nacht" / "heute Nacht" are the same claim without the -s.
            ("um ein Uhr in der Nacht", (1, 0)),
            ("heute Nacht um zwei Uhr", (2, 0)),
        ],
    )
    def test_nachts_keeps_small_hours_am(self, utterance, expected):
        parsed = parse_relative_datetime_de(utterance, now=NOW)
        assert parsed is not None, f"{utterance!r} did not parse"
        assert (parsed.dt.hour, parsed.dt.minute) == expected

    def test_nachts_past_small_hour_rolls_to_tomorrow(self):
        # NOW is 10:00, so 01:00 today is past — it must not silently become 13:00.
        parsed = parse_relative_datetime_de("um ein Uhr nachts", now=NOW)
        assert parsed is not None
        assert (parsed.dt.hour, parsed.dt.minute) == (1, 0)
        assert parsed.dt.date() == datetime(2025, 8, 16).date()

    def test_morgens_still_wins_over_nacht(self):
        parsed = parse_relative_datetime_de("um drei Uhr morgens", now=NOW)
        assert parsed is not None
        assert parsed.dt.hour == 3

    @pytest.mark.parametrize("utterance", ["eine Übernachtung um drei", "um Mitternacht"])
    def test_nacht_inside_another_word_is_not_a_night_marker(self, utterance):
        # \b guards these: "Übernachtung" must keep the business-hours default,
        # and "Mitternacht" has no parseable clock time at all.
        parsed = parse_relative_datetime_de(utterance, now=NOW)
        if parsed is not None:
            assert parsed.dt.hour == 15

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

    @pytest.mark.parametrize(
        "utterance",
        [
            "Nicht ganz richtig",
            "Das ist nicht ganz richtig",
            "Nicht so ganz richtig",
            "Nicht wirklich korrekt",
            "Nicht ganz genau",
            "Das ist nicht in Ordnung",
        ],
    )
    def test_qualified_rejections_are_not_confirmations(self, utterance):
        """An intervening word used to defeat the "(?<!nicht )" lookaround, so
        "nicht ganz richtig" confirmed the very action the caller rejected."""
        assert parse_confirmation(utterance) == ConfirmationStatus.REJECTED

    def test_unclear_reasks(self):
        assert parse_confirmation("Hmm, vielleicht") == ConfirmationStatus.UNCLEAR
        assert parse_confirmation("") == ConfirmationStatus.UNCLEAR

    def test_english_still_works(self):
        assert parse_confirmation("Yes, go ahead") == ConfirmationStatus.CONFIRMED
        assert parse_confirmation("No, stop") == ConfirmationStatus.REJECTED
