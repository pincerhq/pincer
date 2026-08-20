"""Sprint 11 §7 — deterministic speech rendering of tool results."""

from __future__ import annotations

import pytest

from pincer.voice import tool_speech as ts
from pincer.voice.prompts import de as de_pack
from pincer.voice.prompts import en as en_pack

FREEBUSY = (
    "primary: BUSY at:\n    2026-08-18T09:00:00+02:00 → 2026-08-18T10:00:00+02:00\n"
    "    2026-08-18T15:00:00+02:00 → 2026-08-18T16:00:00+02:00"
)
ARGS = {"emails": "primary", "time_min": "2026-08-18T08:00:00+02:00", "time_max": "2026-08-18T18:00:00+02:00"}


@pytest.mark.parametrize("language", ["en", "de", "uk"])
def test_freebusy_free_slots_rendered_without_raw_data(language):
    text = ts.render("google__check_freebusy", FREEBUSY, language, ARGS)
    assert ts.is_speakable(text), text
    head = {"en": "Free would be:", "de": "Frei wäre:", "uk": "Вільно:"}[language]
    assert text.startswith(head)
    if language == "de":
        assert "Dienstag, der achtzehnte August von acht Uhr bis neun Uhr" in text


def test_freebusy_all_free_and_none_free():
    assert (
        ts.render("google__check_freebusy", "primary: FREE", "de", ARGS)
        == de_pack.TOOL_SPEECH["google__check_freebusy.all_free"]
    )
    packed = "primary: BUSY at:\n    2026-08-18T08:00:00+02:00 → 2026-08-18T18:00:00+02:00"
    assert (
        ts.render("google__check_freebusy", packed, "en", ARGS)
        == en_pack.TOOL_SPEECH["google__check_freebusy.none_free"]
    )


def test_freebusy_unknown_output_is_an_error_line():
    assert ts.render("google__check_freebusy", "Auth error: token expired", "de", ARGS) == de_pack.TOOL_ERROR
    assert ts.render("google__check_freebusy", "garbage", "de", ARGS) == de_pack.TOOL_ERROR


def test_list_events_spoken():
    raw = (
        "2 event(s) (2026-08-18 – 2026-08-25):\n"
        "  2026-08-18T09:00:00+02:00 → 2026-08-18T10:00:00+02:00 — Zahnarzt | Praxis | 2 attendee(s)\n  ID: a\n"
        "  2026-08-19T11:00:00+02:00 → 2026-08-19T12:00:00+02:00 — Steuerberater\n  ID: b"
    )
    de = ts.render("google__list_events", raw, "de")
    assert de.startswith("Es stehen 2 Termin(e) an:")
    assert "Dienstag, der achtzehnte August um neun Uhr: Zahnarzt" in de
    assert "|" not in de and "ID:" not in de
    en = ts.render("google__list_events", raw, "en")
    assert "Tuesday, August 18 at 9:00 AM: Zahnarzt" in en
    assert (
        ts.render("google__list_events", "No events between 2026-08-18 and 2026-08-25.", "de")
        == (de_pack.TOOL_SPEECH["google__list_events.none"])
    )


def test_create_update_and_owner_tools():
    assert ts.render("google__create_event", "Event created: 'X'\nID: 1", "de") == "Der Termin ist eingetragen."
    assert ts.render("google__create_event", "Event already exists (idempotent): 'X'", "en") == (
        "That appointment was already in the calendar."
    )
    assert ts.render("google__update_event", "Event updated: 'X' (id=1)", "de") == "Der Termin ist aktualisiert."
    assert ts.render("send_owner_message", "Message delivered to the user.", "de") == "Die Nachricht ist weitergegeben."
    assert ts.render("memory_note", "Note stored.", "en") == "Noted."
    assert ts.render("unknown_tool", "whatever {json}", "en") == "Done."


def test_contact_and_memory_search():
    contacts = '[{"name": "Dr. Müller", "phone_number": "+49301234"}, {"name": "Dr. Meier", "phone_number": "+4930"}]'
    text = ts.render("contact_lookup", contacts, "de")
    assert text == "Kontakt gefunden: Dr. Müller und Dr. Meier."
    assert ts.render("contact_lookup", "[]", "en") == "I have no contact on file for that."
    assert ts.render("memory_search", "- prefers mornings\n- 2026-08-18T10:00 said hello", "en").startswith(
        "Noted earlier: prefers mornings and said hello"
    )


def test_error_results_become_the_error_line():
    assert ts.render("google__create_event", "Error: 403", "de") == de_pack.TOOL_ERROR
    assert ts.render("google__create_event", "", "en") == en_pack.TOOL_ERROR


def test_ensure_speakable_blocks_braces_and_iso():
    assert ts.ensure_speakable("Der Termin {x} ist da", "de") == de_pack.TOOL_ERROR
    assert ts.ensure_speakable("Start 2026-08-18T14:00:00", "en") == en_pack.TOOL_ERROR
    assert ts.ensure_speakable("Alles gut.", "de") == "Alles gut."


def test_describe_action_verify_commitment():
    args = {"summary": "Beratung", "start": "2026-08-18T14:00:00+02:00", "end": "2026-08-18T14:30:00+02:00"}
    de = ts.describe_action("google__create_event", args, "de")
    assert de == "den Termin „Beratung“ am Dienstag, der achtzehnte August um vierzehn Uhr eintragen"
    en = ts.describe_action("google__create_event", args, "en")
    assert en == "create the appointment 'Beratung' on Tuesday, August 18 at 2:00 PM"
    assert ts.describe_action("send_owner_message", {"text": "Sie ruft zurück"}, "de") == (
        "an meinen Nutzer weitergeben: Sie ruft zurück"
    )
    assert ts.describe_action("memory_note", {"note": "prefers mornings"}, "en") == "make a note of: prefers mornings"
    assert ts.describe_action("some__other", {"a": 1}, "en") == "carry out some other"
    for lang in ("en", "de", "uk"):
        assert ts.is_speakable(ts.describe_action("google__update_event", {"event_id": "e", **args}, lang))


def test_spoken_datetime_forms():
    from datetime import datetime

    dt = datetime.fromisoformat("2026-08-18T14:30:00+02:00")
    assert ts.spoken_datetime(dt, "de") == "Dienstag, der achtzehnte August um vierzehn Uhr dreißig"
    assert ts.spoken_datetime(dt, "en") == "Tuesday, August 18 at 2:30 PM"
    assert ts.spoken_datetime(dt, "uk") == "вівторок, 18 серпня о 14:30"
    assert not ts.RAW_DATA_RE.search(ts.spoken_datetime(dt, "uk"))
