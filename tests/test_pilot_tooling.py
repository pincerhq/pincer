"""Pilot onboarding and review tooling (Sprint 10, T10.1–T10.3).

Two things carry real risk here and get the most attention:

* the onboarding step data, because it is what the ≤2h target is measured
  against and an automation ranking built on wrong minutes is worse than none;
* the fixture export, because a fixture is a file that gets committed, so a PII
  leak in one is permanent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import aiosqlite
import pytest

from pincer.observability.pilot_review import (
    detect_name_risks,
    export_persona_fixture,
    render_spot_check,
    sample_calls,
)
from pincer.onboarding import (
    STEPS,
    StepStatus,
    automatable_minutes,
    automation_candidates,
    blocking,
    manual_minutes,
    preflight,
    render_checklist,
    total_minutes,
)
from pincer.voice.retention import ensure_voice_tables


@pytest.fixture
def settings(tmp_path) -> MagicMock:
    cfg = MagicMock()
    cfg.db_path = str(tmp_path / "pincer.db")
    cfg.data_dir = tmp_path
    cfg.twilio_account_sid = "AC123"
    cfg.twilio_auth_token.get_secret_value.return_value = "tok"
    cfg.twilio_phone_number = "+4930123456"
    cfg.voice_webhook_base_url = "https://voice.example.com"
    cfg.dashboard_token.get_secret_value.return_value = "d" * 32
    cfg.web_chat_token.get_secret_value.return_value = "w" * 32
    cfg.elevenlabs_api_key.get_secret_value.return_value = "el"
    cfg.elevenlabs_voice_id_de = "voice-de"
    cfg.elevenlabs_voice_id_en = ""
    cfg.elevenlabs_voice_id_uk = ""
    cfg.elevenlabs_voice_id = ""
    cfg.voice_default_language = "de"
    cfg.voice_consent_mode = "two_party"
    cfg.voice_transcript_retention_days = 90
    cfg.voice_timezone = "Europe/Berlin"
    cfg.timezone = "Europe/Berlin"
    cfg.ops_user_id = "ops"
    cfg.ops_alert_email = ""
    cfg.voice_canary_enabled = True
    cfg.voice_canary_number = "+4915100000001"
    cfg.voice_daily_call_limit = 20
    cfg.voice_quiet_hours = "20:00-08:00"
    # The calendar check looks for a real OAuth token file under data_dir.
    (tmp_path / "google_token.json").write_text("{}")
    return cfg


# ── Onboarding step data ─────────────────────────────────────────────


def test_every_step_has_a_time_estimate():
    """The ≤2h target is measured against these; a zero would silently pass it."""
    for step in STEPS:
        assert step.minutes > 0, f"{step.key} has no time estimate"
        assert step.title


def test_manual_steps_declare_an_automation_position():
    """Every manual step either says how to automate it, or why it cannot be."""
    for step in STEPS:
        if step.automated:
            continue
        assert step.automation_note, f"{step.key} is manual with no automation note"


def test_automation_candidates_exclude_the_inherently_human():
    """Signing a contract is not an automation candidate."""
    keys = {s.key for s in automation_candidates()}
    assert "contract" not in keys
    assert "handover" not in keys
    assert keys  # but there ARE candidates


def test_automation_candidates_are_ranked_by_minutes_saved():
    minutes = [s.minutes for s in automation_candidates()]
    assert minutes == sorted(minutes, reverse=True)


def test_totals_are_consistent():
    assert total_minutes() == sum(s.minutes for s in STEPS)
    assert manual_minutes() == sum(s.minutes for s in STEPS if not s.automated)
    assert automatable_minutes() <= manual_minutes()


def test_step_keys_are_unique():
    keys = [s.key for s in STEPS]
    assert len(keys) == len(set(keys))


# ── Preflight ────────────────────────────────────────────────────────


def test_fully_configured_instance_has_no_blocking_items(settings):
    results = preflight(settings)
    assert blocking(results) == []


def test_preflight_flags_a_tunnel_webhook(settings):
    """A pilot on an ngrok URL is a pilot that breaks when the laptop sleeps."""
    settings.voice_webhook_base_url = "https://abc123.ngrok-free.app"
    result = next(r for r in preflight(settings) if r.step.key == "webhook")
    assert result.status is StepStatus.MISSING
    assert "tunnel" in result.message


def test_preflight_flags_identical_api_tokens(settings):
    settings.web_chat_token.get_secret_value.return_value = "d" * 32
    result = next(r for r in preflight(settings) if r.step.key == "api_tokens")
    assert result.status is StepStatus.MISSING
    assert "differ" in result.message


def test_preflight_flags_missing_twilio_config(settings):
    settings.twilio_phone_number = ""
    result = next(r for r in preflight(settings) if r.step.key == "twilio")
    assert result.status is StepStatus.MISSING
    assert "PHONE_NUMBER" in result.message


def test_preflight_notes_a_non_german_number(settings):
    """Not blocking — but a DACH pilot on a +1 number deserves a second look."""
    settings.twilio_phone_number = "+14155551234"
    result = next(r for r in preflight(settings) if r.step.key == "twilio")
    assert result.status is StepStatus.READY
    assert "+49" in result.message


def test_preflight_flags_weakened_compliance(settings):
    settings.voice_consent_mode = "one_party"
    result = next(r for r in preflight(settings) if r.step.key == "compliance")
    assert result.status is StepStatus.MISSING


def test_preflight_requires_a_voice_for_the_default_language(settings):
    """An English voice on a German-first pilot is not a configured voice."""
    settings.elevenlabs_voice_id_de = ""
    settings.elevenlabs_voice_id_en = "voice-en"
    result = next(r for r in preflight(settings) if r.step.key == "voice")
    assert result.status is StepStatus.MISSING
    assert "de" in result.message


def test_preflight_flags_no_voice_at_all(settings):
    settings.elevenlabs_voice_id_de = ""
    result = next(r for r in preflight(settings) if r.step.key == "voice")
    assert result.status is StepStatus.MISSING
    assert "no voice ID configured" in result.message


def test_preflight_flags_alerts_with_no_recipient(settings):
    settings.ops_user_id = ""
    settings.default_user_id = ""
    settings.ops_alert_email = ""
    result = next(r for r in preflight(settings) if r.step.key == "ops")
    assert result.status is StepStatus.MISSING


def test_steps_without_a_check_are_manual_not_ready(settings):
    """An unverifiable step must never be reported as done."""
    for result in preflight(settings):
        if result.step.check is None:
            assert result.status is StepStatus.MANUAL


def test_a_broken_check_does_not_crash_preflight(settings, monkeypatch):
    from pincer import onboarding

    def _boom(_settings):
        raise RuntimeError("exploded")

    monkeypatch.setattr(onboarding.STEPS[2], "check", _boom, raising=False)
    results = preflight(settings)
    assert any(r.status is StepStatus.MISSING and "check failed" in r.message for r in results)


def test_checklist_includes_a_time_tracking_table(settings):
    markdown = render_checklist("Zahnarztpraxis Weber", preflight(settings))
    assert "Zahnarztpraxis Weber" in markdown
    assert "Record the actual time" in markdown
    for step in STEPS:
        assert step.title in markdown


# ── Spot-check sampling ──────────────────────────────────────────────


async def _seed(settings, count: int, *, language: str = "de", code: str = "none") -> None:
    started = datetime.now(UTC) - timedelta(hours=2)
    async with aiosqlite.connect(settings.db_path) as db:
        await ensure_voice_tables(db)
        for i in range(count):
            sid = f"CA{language}{code}{i:03d}"
            await db.execute(
                "INSERT INTO voice_calls (call_sid, direction, started_at, ended_at, failure_code, language, "
                "from_number, to_number) VALUES (?, 'outbound', ?, ?, ?, ?, '+4915100000001', '+4930111222333')",
                (sid, started.isoformat(), (started + timedelta(seconds=90)).isoformat(), code, language),
            )
            await db.execute(
                "INSERT INTO call_transcripts (call_id, speaker, text, is_final, state, timestamp) "
                "VALUES (?, 'caller', ?, 1, '', ?)",
                (sid, f"Hallo, hier ist die Praxis {i}. Meine Nummer ist +4930111222333.", started.isoformat()),
            )
            await db.execute(
                "INSERT INTO call_transcripts (call_id, speaker, text, is_final, state, timestamp) "
                "VALUES (?, 'agent', 'Guten Tag, geht Dienstag um drei?', 1, 'freeform', ?)",
                (sid, (started + timedelta(seconds=5)).isoformat()),
            )
        await db.commit()


async def test_sampling_is_deterministic_from_the_seed(settings):
    """Two reviewers with the same seed must see the same calls."""
    await _seed(settings, 40)
    first = await sample_calls(settings, count=10, seed=3)
    second = await sample_calls(settings, count=10, seed=3)
    assert [c.call_sid for c in first] == [c.call_sid for c in second]


async def test_different_seeds_give_different_samples(settings):
    await _seed(settings, 40)
    first = {c.call_sid for c in await sample_calls(settings, count=10, seed=1)}
    second = {c.call_sid for c in await sample_calls(settings, count=10, seed=2)}
    assert first != second


async def test_sampling_returns_everything_when_below_count(settings):
    await _seed(settings, 4)
    assert len(await sample_calls(settings, count=10)) == 4


async def test_sample_masks_phone_numbers(settings):
    """A review sheet gets shared; raw numbers must not travel with it."""
    await _seed(settings, 3)
    call = (await sample_calls(settings, count=3))[0]
    assert "…" in call.from_number
    assert "4915100000001" not in call.from_number
    joined = " ".join(line["text"] for line in call.transcript)
    assert "+4930111222333" not in joined


async def test_failures_only_filter(settings):
    await _seed(settings, 5, code="none")
    await _seed(settings, 3, code="ws_drop")
    calls = await sample_calls(settings, count=10, only_failures=True)
    assert len(calls) == 3
    assert all(c.failure_code == "ws_drop" for c in calls)


async def test_language_filter(settings):
    await _seed(settings, 4, language="de")
    await _seed(settings, 2, language="en")
    calls = await sample_calls(settings, count=10, language="en")
    assert len(calls) == 2


async def test_empty_database_samples_nothing(settings):
    assert await sample_calls(settings, count=10) == []


async def test_spot_check_sheet_carries_the_review_questions(settings):
    await _seed(settings, 2)
    sheet = render_spot_check(await sample_calls(settings, count=2), week="week 2")
    assert "week 2" in sheet
    assert "asr_accuracy" in sheet
    assert "would_ship" in sheet
    assert "export-fixture" in sheet  # tells the reviewer what to do with a miss


def test_spot_check_sheet_handles_no_calls():
    assert "No calls" in render_spot_check([])


# ── Fixture export ───────────────────────────────────────────────────


async def test_export_builds_a_replayable_persona(settings):
    await _seed(settings, 1)
    fixture = await export_persona_fixture(settings, "CAdenone000", name="praxis_de")
    assert fixture["name"] == "praxis_de"
    assert fixture["opening"]
    assert fixture["source_call_sid"] == "CAdenone000"
    assert fixture["agent_context"]  # the conversation the callee reacted to


async def test_export_masks_pii_in_the_script(settings):
    """A fixture is a committed file — a leak in one is permanent."""
    await _seed(settings, 1)
    fixture = await export_persona_fixture(settings, "CAdenone000")
    assert "+4930111222333" not in fixture["opening"]


async def test_export_refuses_a_call_with_no_transcript(settings):
    async with aiosqlite.connect(settings.db_path) as db:
        await ensure_voice_tables(db)
        await db.execute(
            "INSERT INTO voice_calls (call_sid, direction, started_at, ended_at, failure_code) "
            "VALUES ('CAempty', 'outbound', ?, ?, 'no_answer')",
            (datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()),
        )
        await db.commit()
    with pytest.raises(ValueError, match="No stored transcript"):
        await export_persona_fixture(settings, "CAempty")


async def test_export_refuses_a_call_with_only_agent_turns(settings):
    started = datetime.now(UTC).isoformat()
    async with aiosqlite.connect(settings.db_path) as db:
        await ensure_voice_tables(db)
        await db.execute(
            "INSERT INTO voice_calls (call_sid, direction, started_at, ended_at, failure_code) "
            "VALUES ('CAagent', 'outbound', ?, ?, 'silent_callee')",
            (started, started),
        )
        await db.execute(
            "INSERT INTO call_transcripts (call_id, speaker, text, is_final, state, timestamp) "
            "VALUES ('CAagent', 'agent', 'Hallo?', 1, '', ?)",
            (started,),
        )
        await db.commit()
    with pytest.raises(ValueError, match="no callee turns"):
        await export_persona_fixture(settings, "CAagent")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Hier ist Frau Schneider", "Schneider"),
        ("this is Michael speaking", "Michael"),
        ("Guten Tag, mein Name ist Weber", "Weber"),
        ("Dr. Braun am Apparat", "Braun"),
    ],
)
def test_name_detection_flags_likely_names(text, expected):
    """mask_pii cannot catch names — they are not a pattern. Flag them instead."""
    assert expected in detect_name_risks([{"text": text}])


def test_name_detection_is_quiet_on_ordinary_speech():
    lines = [{"text": "Ja, Dienstag um drei passt gut."}, {"text": "Vielen Dank, auf Wiederhören."}]
    assert detect_name_risks(lines) == []


async def test_export_surfaces_names_for_human_review(settings):
    started = datetime.now(UTC).isoformat()
    async with aiosqlite.connect(settings.db_path) as db:
        await ensure_voice_tables(db)
        await db.execute(
            "INSERT INTO voice_calls (call_sid, direction, started_at, ended_at, failure_code, language) "
            "VALUES ('CAname', 'outbound', ?, ?, 'none', 'de')",
            (started, started),
        )
        await db.execute(
            "INSERT INTO call_transcripts (call_id, speaker, text, is_final, state, timestamp) "
            "VALUES ('CAname', 'caller', 'Praxis Dr. Schneider, guten Tag', 1, '', ?)",
            (started,),
        )
        await db.commit()

    fixture = await export_persona_fixture(settings, "CAname")
    assert "Schneider" in fixture["review_required"]["possible_names"]
    assert "placeholder" in fixture["review_required"]["instruction"].lower()
