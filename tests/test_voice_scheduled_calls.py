"""Calls the owner asked for later (POST /api/voice/calls/scheduled).

The load-bearing behaviour: the number and the briefing are validated when the
schedule is created rather than when it fires — a call refused at 23:10 with
nobody watching is the failure this endpoint exists to avoid — and what fires
goes through the ordinary outbound path, guardrails included.
"""

import os
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

os.environ.setdefault("PINCER_ANTHROPIC_API_KEY", "sk-ant-test-key")

from fastapi.testclient import TestClient

from pincer.api.server import create_app
from pincer.voice.scheduled_calls import (
    ScheduledCallError,
    build_action,
    make_scheduled_call_handler,
    one_off_cron,
    resolve_when,
)

BERLIN = ZoneInfo("Europe/Berlin")
LATE = datetime(2026, 8, 21, 23, 50, tzinfo=BERLIN)


@pytest.fixture
def client(monkeypatch, tmp_path):
    from pincer.config import get_settings_relaxed

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PINCER_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PINCER_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PINCER_WEB_CHAT_TOKEN", raising=False)
    get_settings_relaxed.cache_clear()
    app = create_app()
    yield TestClient(app)
    get_settings_relaxed.cache_clear()


# ── When ─────────────────────────────────────────────────────────────


def test_minutes_from_now_cross_midnight():
    when = resolve_when(tz="Europe/Berlin", run_in_minutes=20, now=LATE)
    assert when.isoformat() == "2026-08-22T00:10:00+02:00"
    # The date rolls with it — the arithmetic a date picker leaves to the user.
    assert one_off_cron(when) == "10 0 22 8 *"


def test_naive_time_is_read_in_the_callers_timezone():
    when = resolve_when(tz="Europe/Berlin", at="2026-08-22 09:15", now=LATE)
    assert when.utcoffset() == timedelta(hours=2)
    assert when.hour == 9 and when.minute == 15


def test_seconds_are_dropped_because_cron_cannot_keep_them():
    when = resolve_when(tz="UTC", at="2026-08-22T09:15:42", now=LATE)
    assert when.second == 0


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"run_in_minutes": 0}, "at least"),
        ({"at": "2020-01-01 09:00"}, "already passed"),
        ({"run_in_minutes": 5, "at": "2026-08-22 09:15"}, "exactly one"),
        ({}, "exactly one"),
        ({"at": "not a time"}, "Could not read"),
        ({"run_in_minutes": 60 * 24 * 365}, "within the next"),
    ],
)
def test_refuses_impossible_moments(kwargs, message):
    with pytest.raises(ScheduledCallError) as err:
        resolve_when(tz="Europe/Berlin", now=LATE, **kwargs)
    assert message in str(err.value)


# ── The endpoint ─────────────────────────────────────────────────────


def test_schedules_a_call_and_lists_it(client):
    created = client.post(
        "/api/voice/calls/scheduled",
        json={
            "target_number": "+4930111222",
            "purpose": "Ask whether the delivery arrived and confirm the address.",
            "run_in_minutes": 30,
            "target_name": "Anna",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["target_number"] == "+4930111222"
    assert body["next_run_at"]  # the scheduler resolved a moment

    listed = client.get("/api/voice/calls/scheduled").json()
    assert [c["id"] for c in listed] == [body["id"]]
    assert listed[0]["purpose"].startswith("Ask whether the delivery")


def test_refuses_a_purpose_the_agent_could_not_open_with(client):
    r = client.post(
        "/api/voice/calls/scheduled",
        json={"target_number": "+4930111222", "purpose": "call mum", "run_in_minutes": 30},
    )
    # Same gate as an immediate call, and the same sentence.
    assert r.status_code == 422
    assert "Purpose too short" in r.json()["detail"]


def test_refuses_a_number_twilio_would_reject(client):
    r = client.post(
        "/api/voice/calls/scheduled",
        json={"target_number": "030 123", "purpose": "Ask about the invoice from July.", "run_in_minutes": 5},
    )
    assert r.status_code == 422
    assert "Invalid phone number" in r.json()["detail"]


def test_refuses_a_moment_that_has_passed(client):
    r = client.post(
        "/api/voice/calls/scheduled",
        json={
            "target_number": "+4930111222",
            "purpose": "Ask about the invoice from July.",
            "at": "2020-01-01 09:00",
        },
    )
    assert r.status_code == 422
    assert "already passed" in r.json()["detail"]


def test_cancels_a_scheduled_call(client):
    sid = client.post(
        "/api/voice/calls/scheduled",
        json={
            "target_number": "+4930111222",
            "purpose": "Ask about the invoice from July.",
            "run_in_minutes": 45,
        },
    ).json()["id"]

    assert client.delete(f"/api/voice/calls/scheduled/{sid}").status_code == 204
    assert client.get("/api/voice/calls/scheduled").json() == []
    # Cancelling twice is a 404, not a silent success.
    assert client.delete(f"/api/voice/calls/scheduled/{sid}").status_code == 404


def test_scheduled_is_not_read_as_a_call_sid(client):
    """The listing route must win over /calls/{call_sid}."""
    assert client.get("/api/voice/calls/scheduled").status_code == 200


# ── What happens when it fires ───────────────────────────────────────


@pytest.mark.asyncio
async def test_firing_places_the_call_through_the_normal_path(monkeypatch):
    from pincer.config import get_settings_relaxed

    seen = {}

    async def fake_make_phone_call(**kwargs):
        seen.update(kwargs)
        return "Call initiated. Call SID: CA123"

    monkeypatch.setattr("pincer.voice.outbound.make_phone_call", fake_make_phone_call)
    handler = make_scheduled_call_handler(get_settings_relaxed())

    action = build_action(
        target_number="+4930111222",
        purpose="Ask whether the delivery arrived.",
        target_name="Anna",
        thread_id="th_1",
    )
    message = await handler(pincer_user_id="dashboard", action=action, channel="web")

    assert seen["target_number"] == "+4930111222"
    assert seen["purpose"] == "Ask whether the delivery arrived."
    assert seen["thread_id"] == "th_1"
    # Marked as the scheduler's, so the briefing records where it came from.
    assert seen["source"] == "scheduler"
    assert "Placing the scheduled call" in message


@pytest.mark.asyncio
async def test_a_refused_call_is_reported_not_swallowed(monkeypatch):
    from pincer.config import get_settings_relaxed

    async def refuses(**kwargs):
        return "Error: quiet hours are active until 08:00"

    monkeypatch.setattr("pincer.voice.outbound.make_phone_call", refuses)
    handler = make_scheduled_call_handler(get_settings_relaxed())

    message = await handler(
        pincer_user_id="dashboard",
        action=build_action(target_number="+4930111222", purpose="Ask about the invoice."),
        channel="web",
    )
    # The owner asked for this call; a guardrail stopping it is news.
    assert "was refused" in message
    assert "quiet hours" in message


@pytest.mark.asyncio
async def test_a_crash_at_fire_time_still_tells_the_owner(monkeypatch):
    from pincer.config import get_settings_relaxed

    async def explodes(**kwargs):
        raise RuntimeError("twilio unreachable")

    monkeypatch.setattr("pincer.voice.outbound.make_phone_call", explodes)
    handler = make_scheduled_call_handler(get_settings_relaxed())

    message = await handler(
        pincer_user_id="dashboard",
        action=build_action(target_number="+4930111222", purpose="Ask about the invoice."),
        channel="web",
    )
    assert "could not be placed" in message


@pytest.mark.asyncio
async def test_the_actor_knows_the_action_type():
    """A schedule with no handler is a call that silently never happens."""
    from pincer.voice.scheduled_calls import ACTION_TYPE

    source = (
        __import__("pathlib").Path(__file__).resolve().parents[1] / "src/pincer/tasks/actors.py"
    ).read_text()
    assert f'"{ACTION_TYPE}": make_scheduled_call_handler' in source
