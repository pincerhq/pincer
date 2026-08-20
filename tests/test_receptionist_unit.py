"""Sprint 12 — receptionist unit tests (§14 unit list): profile schema, hours/DST,
intent token parsing, slot spell-back threshold, number read-back, inbound
tool set, free/busy windows-only rendering, report template, config parsing."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from pincer.exceptions import ConfigError
from pincer.voice.receptionist import intents, slots
from pincer.voice.receptionist.profile import (
    BusinessProfile,
    ProfileError,
    load_business_profile,
    parse_business_profile,
)

BERLIN = ZoneInfo("Europe/Berlin")

VALID = {
    "version": 1,
    "business": {"name": "Praxis Dr. Müller", "languages": ["de", "en"], "timezone": "Europe/Berlin"},
    "hours": {
        "mon": ["08:00-12:00", "14:00-17:00"],
        "tue": ["08:00-12:00", "14:00-17:00"],
        "wed": ["08:00-12:00"],
        "thu": ["08:00-12:00", "14:00-17:00"],
        "fri": ["08:00-12:00"],
        "sat": [],
        "sun": [],
    },
    "services": ["Allgemeinmedizin", "Vorsorgeuntersuchungen"],
    "faq": [
        {"q": "Wo kann ich parken?", "a": "Direkt hinter dem Haus, Einfahrt Gartenstraße."},
        {"q": "Nehmen Sie neue Patienten auf?", "a": "Ja, aktuell nehmen wir neue Patienten auf."},
    ],
    "address": "Gartenstraße 12, 32257 Bünde",
    "booking": {
        "enabled": True,
        "event_duration_min": 30,
        "ask_email": False,
        "event_title_template": "Termin: {caller_name}",
    },
    "transfer": {"enabled": True, "target": "+4952233344455", "announce": "Ich verbinde Sie."},
    "after_hours": {"message": "Sie erreichen uns Montag bis Freitag vormittags. Ich nehme gern eine Nachricht auf."},
}


def _with(**changes):
    import copy

    data = copy.deepcopy(VALID)
    for dotted, value in changes.items():
        cursor = data
        parts = dotted.split(".")
        for part in parts[:-1]:
            cursor = cursor[part]
        if value is ...:
            del cursor[parts[-1]]
        else:
            cursor[parts[-1]] = value
    return data


# ── §4 schema validation ─────────────────────────────────────────────


def test_valid_profile_loads():
    profile = parse_business_profile(VALID)
    assert isinstance(profile, BusinessProfile)
    assert profile.default_language == "de"
    assert profile.transfer.target == "+4952233344455"
    assert profile.hours_for(0) == [
        (datetime(2026, 1, 1, 8).time(), datetime(2026, 1, 1, 12).time()),
        (datetime(2026, 1, 1, 14).time(), datetime(2026, 1, 1, 17).time()),
    ]


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"version": 2}, "version"),
        ({"business.name": ""}, "business.name"),
        ({"business.name": "x" * 81}, "business.name"),
        ({"business.languages": []}, "business.languages"),
        ({"business.languages": ["fr"]}, "business.languages"),
        ({"business.timezone": "Mars/Olympus"}, "business.timezone"),
        ({"hours.mon": ["08:00-07:00"]}, "hours"),
        ({"hours.mon": ["8-12"]}, "hours"),
        ({"hours.mon": ["08:00-12:00", "11:00-13:00"]}, "hours"),
        ({"hours.sun": ...}, "hours"),
        ({"hours.funday": []}, "hours"),
        ({"faq": [{"q": "x", "a": "y" * 301}]}, "faq"),
        ({"faq": [{"q": "x", "a": "y"}] * 51}, "faq"),
        ({"booking.event_duration_min": 1}, "booking.event_duration_min"),
        ({"transfer.target": "0522333"}, "transfer"),
        ({"transfer.target": ""}, "transfer"),
    ],
    ids=lambda v: str(v)[:40],
)
def test_profile_schema_validation(changes, field):
    """Each invalid field individually → ProfileError naming the field."""
    with pytest.raises(ProfileError) as excinfo:
        parse_business_profile(_with(**changes))
    assert field.split(".")[0] in str(excinfo.value)


def test_transfer_disabled_needs_no_target():
    profile = parse_business_profile(_with(**{"transfer.enabled": False, "transfer.target": ""}))
    assert not profile.transfer.enabled


def test_load_from_yaml_and_missing_file(tmp_path):
    import yaml

    path = tmp_path / "business_profile.yaml"
    path.write_text(yaml.safe_dump(VALID, allow_unicode=True), encoding="utf-8")
    assert load_business_profile(path).business.name == "Praxis Dr. Müller"
    with pytest.raises(ProfileError, match="not found"):
        load_business_profile(tmp_path / "nope.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: [1\n", encoding="utf-8")
    with pytest.raises(ProfileError, match="YAML"):
        load_business_profile(bad)


def test_load_from_settings_enabled_and_disabled(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import yaml

    from pincer.voice.receptionist import profile as prof

    path = tmp_path / "p.yaml"
    path.write_text(yaml.safe_dump(VALID, allow_unicode=True), encoding="utf-8")
    assert prof.load_from_settings(SimpleNamespace(receptionist_enabled=False, business_profile=str(path))) is None
    assert prof.get_profile() is None
    loaded = prof.load_from_settings(SimpleNamespace(receptionist_enabled=True, business_profile=str(path)))
    assert loaded is not None and prof.get_profile() is loaded
    assert prof.receptionist_active(SimpleNamespace(receptionist_enabled=True))
    with pytest.raises(ProfileError):
        prof.load_from_settings(SimpleNamespace(receptionist_enabled=True, business_profile=str(tmp_path / "x.yaml")))
    prof.set_profile(None)


# ── hours open/closed incl. DST ──────────────────────────────────────


def test_hours_open_closed_dst():
    profile = parse_business_profile(VALID)
    # Monday in CET (winter) and CEST (summer): wall-clock 08:00 is open either way
    assert profile.is_open(datetime(2026, 1, 12, 8, 0, tzinfo=BERLIN))  # Mon, CET
    assert profile.is_open(datetime(2026, 7, 13, 8, 0, tzinfo=BERLIN))  # Mon, CEST
    assert not profile.is_open(datetime(2026, 7, 13, 7, 59, tzinfo=BERLIN))
    assert not profile.is_open(datetime(2026, 7, 13, 12, 0, tzinfo=BERLIN))  # lunch gap
    assert profile.is_open(datetime(2026, 7, 13, 14, 0, tzinfo=BERLIN))
    assert not profile.is_open(datetime(2026, 7, 13, 17, 0, tzinfo=BERLIN))
    assert not profile.is_open(datetime(2026, 7, 18, 10, 0, tzinfo=BERLIN))  # Saturday closed
    # A UTC instant is converted: 06:30Z on a summer Monday is 08:30 Berlin → open
    assert profile.is_open(datetime(2026, 7, 13, 6, 30, tzinfo=ZoneInfo("UTC")))
    # ...but 06:30Z on a WINTER Monday is 07:30 Berlin → closed (DST-safe)
    assert not profile.is_open(datetime(2026, 1, 12, 6, 30, tzinfo=ZoneInfo("UTC")))
    assert "Montag 8 bis 12 Uhr und 14 bis 17 Uhr" in profile.speakable_hours("de")


# ── §7.2 intent token ────────────────────────────────────────────────


def test_intent_token_parsing():
    assert intents.parse_intent_token("[INTENT:question] Parken können Sie hinter dem Haus.") == (
        "question",
        "Parken können Sie hinter dem Haus.",
    )
    assert intents.parse_intent_token("[INTENT: Appointment ]\nGerne.") == ("appointment", "Gerne.")
    assert intents.parse_intent_token("[INTENT:banana] hi") == ("unknown", "hi")
    assert intents.parse_intent_token("no token here") == (None, "no token here")
    # mixed: the actionable intent wins
    assert intents.parse_intent_token("[INTENT:question][INTENT:appointment] ok")[0] == "appointment"
    assert intents.parse_intent_token("[INTENT:human] one sec [INTENT:question]")[0] == "human"


# ── §8.2 slots ───────────────────────────────────────────────────────


def test_slot_spellback_trigger_threshold():
    assert not slots.needs_spellback("Müller", 0.95)
    assert slots.needs_spellback("Müller", 0.84)  # low confidence
    assert slots.needs_spellback("Xanthopoulos", 0.99)  # uncommon name
    assert not slots.needs_spellback("Schmidt", None)  # unknown confidence counts as sure
    assert slots.needs_spellback("", 1.0)
    assert slots.spell_out("Müller", "de") == "M-Ü-L-L-E-R"
    assert slots.spell_out("Anna Schmidt", "de") == "A-N-N-A dann S-C-H-M-I-D-T"
    assert slots.normalize_name("Ja, mein Name ist Müller, guten Tag") == "Müller"
    assert slots.normalize_name("This is Peter Xanthopoulos speaking") == "Peter Xanthopoulos"
    assert slots.normalize_name("Hier spricht Frau Dr. Weber") == "Weber"


def test_number_readback_grouping_de():
    assert slots.readback_number("+4917212345678", "de") == (
        "plus vier-neun, eins-sieben, zwei-eins, zwei-drei, vier-fünf, sechs-sieben, acht"
    )
    assert (
        slots.readback_number("017212345678", "de")
        == "null-eins, sieben-zwei, eins-zwei, drei-vier, fünf-sechs, sieben-acht"
    )
    assert slots.readback_number("1234", "en") == "one-two, three-four"
    assert slots.spoken_last4("+4917212345678", "de") == "fünf-sechs, sieben-acht"


def test_extract_digits_and_normalize():
    assert (
        slots.extract_digits("null eins sieben zwei, eins zwei drei vier fünf sechs sieben acht", "de")
        == "017212345678"
    )
    assert slots.extract_digits("oh one seven two one two three four five six seven eight", "en") == "017212345678"
    assert slots.normalize_callback_number("017212345678", "+4930123456") == "+4917212345678"
    assert slots.normalize_callback_number("+4917212345678", "") == "+4917212345678"
    assert slots.normalize_callback_number("12", "+49301") is None


def test_matter_cap_urgency_email():
    short, needs = slots.cap_matter("Ich möchte einen Rückruf wegen der Rechnung.")
    assert not needs and short.startswith("Ich möchte")
    long_text = "wort " * 120
    capped, needs = slots.cap_matter(long_text)
    assert needs and capped.endswith("…") and len(capped) <= slots.MATTER_SUMMARY_CHARS + 1
    assert slots.sounds_urgent("Ich habe starke Schmerzen") and not slots.sounds_urgent("Rechnung")
    assert slots.extract_spelled_email("m punkt mueller at praxis punkt de") == "m.mueller@praxis.de"
    assert slots.extract_spelled_email("nein danke") == ""
    assert slots.spell_email("a.b@c.de", "de") == "A Punkt B at C Punkt D E"


# ── §9 inbound tool set + free/busy windows only ─────────────────────


def test_inbound_toolset_exact(settings):
    from pincer.voice.tool_policy import allowed_tools_for_call

    scope = allowed_tools_for_call(settings, kind="receptionist", direction="inbound")
    assert scope == {"google__check_freebusy", "business_profile_lookup", "google__create_event", "send_owner_message"}
    for absent in ("memory_search", "contact_lookup", "google__list_events", "memory_note", "email_send"):
        assert absent not in scope
    # extras never widen the public line
    settings.voice_tools_extra = "google__list_events,memory_search"
    assert allowed_tools_for_call(settings, kind="receptionist", direction="inbound") == scope


def test_freebusy_returns_windows_only():
    from pincer.voice.tool_speech import RAW_DATA_RE, render

    raw = "primary: BUSY at:\n    2026-08-18T09:00:00+02:00 → 2026-08-18T10:00:00+02:00"
    args = {"emails": "primary", "time_min": "2026-08-18T08:00:00+02:00", "time_max": "2026-08-18T12:00:00+02:00"}
    spoken = render("google__check_freebusy", raw, "de", args)
    assert spoken.startswith("Frei wäre:")
    assert not RAW_DATA_RE.search(spoken)
    for leak in ("Zahnarzt", "attendee", "summary", "primary"):
        assert leak not in spoken


def test_business_profile_lookup_tool_returns_profile_only(settings):
    import asyncio
    import json

    from pincer.tools.registry import ToolRegistry
    from pincer.voice.receptionist import profile as prof
    from pincer.voice.receptionist.tools import register_receptionist_tools

    prof.set_profile(parse_business_profile(VALID))
    try:
        registry = ToolRegistry()
        register_receptionist_tools(registry)
        data = json.loads(
            asyncio.run(registry.execute("business_profile_lookup", {"topic": "faq", "question": "parken"}))
        )
        assert data["faq"] == [{"q": "Wo kann ich parken?", "a": "Direkt hinter dem Haus, Einfahrt Gartenstraße."}]
        data = json.loads(asyncio.run(registry.execute("business_profile_lookup", {})))
        assert set(data) == {"hours", "address", "services", "faq", "business_name"}
    finally:
        prof.set_profile(None)


# ── §12 owner report ─────────────────────────────────────────────────


def test_owner_report_template_de():
    from pincer.voice.receptionist.report import render_owner_report

    profile = parse_business_profile(VALID)
    reception = {
        "intent": "message",
        "slots": {
            "caller_name": "Xanthopoulos",
            "caller_name_unverified": True,
            "callback_number": "+4917212345678",
            "matter": "Rückruf wegen Rechnung",
            "urgent": True,
        },
        "booking": {},
    }
    text = render_owner_report(
        reception,
        call_sid="CA1",
        profile=profile,
        language="de",
        ended_at=datetime(2026, 8, 18, 12, 0, tzinfo=ZoneInfo("UTC")),
    )
    lines = text.split("\n")
    assert lines[0] == "📞 Anruf für Praxis Dr. Müller — 18.08.2026 14:00"
    assert lines[1] == "Von: Xanthopoulos (unbestätigt) · +4917212345678"
    assert lines[2] == "Anliegen: Rückruf wegen Rechnung"
    assert lines[3] == "❗ DRINGEND"
    assert lines[-1] == "📄 Transkript: /transcript CA1"
    booked = render_owner_report(
        {
            "intent": "appointment",
            "slots": {"caller_name": "Meier", "callback_number": "+491"},
            "booking": {
                "booked": True,
                "slot_spoken": "Dienstag, der achtzehnte August um vierzehn Uhr",
                "calendar_link": "https://cal/ev1",
            },
        },
        call_sid="CA2",
        profile=profile,
        language="de",
    )
    assert "📅 Termin gebucht: Dienstag, der achtzehnte August um vierzehn Uhr — https://cal/ev1" in booked
    assert "Sperrliste" in render_owner_report(reception, call_sid="CA3", profile=profile, language="de", abusive=True)


# ── §3 config ────────────────────────────────────────────────────────


def test_receptionist_booking_approval_rejects_verbal():
    from pincer.config import Settings

    with pytest.raises(ConfigError, match="verbal"):
        Settings(anthropic_api_key="sk-ant-test", receptionist_booking_approval="verbal")
    assert (
        Settings(anthropic_api_key="sk-ant-test", receptionist_booking_approval="user").receptionist_booking_approval
        == "user"
    )
    assert Settings(anthropic_api_key="sk-ant-test").receptionist_booking_approval == "off"


def test_doctor_checks(tmp_path):
    from types import SimpleNamespace

    import yaml

    from pincer.security.doctor import CheckStatus, SecurityDoctor

    doc = SecurityDoctor(data_dir=tmp_path, config_dir=tmp_path, skills_dir=tmp_path)
    path = tmp_path / "p.yaml"
    path.write_text(yaml.safe_dump(VALID, allow_unicode=True), encoding="utf-8")
    ok = SimpleNamespace(receptionist_enabled=True, business_profile=str(path), inbound_recording=False)
    assert doc._check_receptionist_profile(ok).status == CheckStatus.PASS
    bad = SimpleNamespace(receptionist_enabled=True, business_profile=str(tmp_path / "missing.yaml"))
    assert doc._check_receptionist_profile(bad).status == CheckStatus.CRITICAL
    off = SimpleNamespace(receptionist_enabled=False)
    assert doc._check_receptionist_profile(off).status == CheckStatus.SKIPPED
    rec_bad = SimpleNamespace(
        receptionist_enabled=True, inbound_recording=True, voice_consent_mode="one_party", voice_recording_enabled=True
    )
    assert doc._check_inbound_recording_consent(rec_bad).status == CheckStatus.CRITICAL
    rec_ok = SimpleNamespace(
        receptionist_enabled=True, inbound_recording=True, voice_consent_mode="two_party", voice_recording_enabled=True
    )
    assert doc._check_inbound_recording_consent(rec_ok).status == CheckStatus.PASS
    assert doc._check_inbound_recording_consent(ok).status == CheckStatus.PASS


def test_failure_codes_for_inbound_declines():
    from pincer.observability.failure_codes import FailureCode, classify_failure, counts_against_slo, describe

    assert classify_failure("busy_capacity") == FailureCode.BUSY_CAPACITY
    assert classify_failure("blocklist") == FailureCode.BLOCKED
    assert not counts_against_slo(FailureCode.BLOCKED) and not counts_against_slo(FailureCode.BUSY_CAPACITY)
    assert "blocklist" in describe("blocked")
