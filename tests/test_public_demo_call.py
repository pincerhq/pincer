"""The website's demo call — the most abusable endpoint in the app.

It dials whoever the request names, with no token, so these tests are mostly
about what it refuses: switched off by default, nothing without consent, and
three independent ceilings so it cannot become a way to make someone else's
phone ring repeatedly.
"""

import os

import pytest

os.environ.setdefault("PINCER_ANTHROPIC_API_KEY", "sk-ant-test-key")

from fastapi.testclient import TestClient

from pincer.api import public_demo
from pincer.api.server import create_app

GOOD = {"phone_number": "+4930111222", "consent": True}


@pytest.fixture
def client(monkeypatch, tmp_path):
    from pincer.config import get_settings_relaxed

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PINCER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PINCER_DEMO_CALL_ENABLED", "true")
    monkeypatch.delenv("PINCER_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PINCER_WEB_CHAT_TOKEN", raising=False)
    get_settings_relaxed.cache_clear()
    public_demo.reset_limits()
    app = create_app()
    yield TestClient(app)
    public_demo.reset_limits()
    get_settings_relaxed.cache_clear()


@pytest.fixture
def dials(monkeypatch):
    """Capture what would have been dialled."""
    calls = []

    async def fake(**kwargs):
        calls.append(kwargs)
        return "Call initiated. Call SID: CA123"

    monkeypatch.setattr("pincer.voice.outbound.make_phone_call", fake)
    return calls


def test_off_by_default(monkeypatch, tmp_path):
    from pincer.config import get_settings_relaxed

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PINCER_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PINCER_DEMO_CALL_ENABLED", raising=False)
    monkeypatch.delenv("PINCER_DASHBOARD_TOKEN", raising=False)
    get_settings_relaxed.cache_clear()
    with TestClient(create_app()) as c:
        assert c.post("/api/public/demo-call", json=GOOD).status_code == 404
    get_settings_relaxed.cache_clear()


def test_needs_no_token(client, dials):
    """The point of the endpoint: a marketing page has no bearer."""
    r = client.post("/api/public/demo-call", json=GOOD)
    assert r.status_code == 202, r.text
    assert dials and dials[0]["target_number"] == "+4930111222"


def test_refuses_without_consent(client, dials):
    r = client.post("/api/public/demo-call", json={"phone_number": "+4930111222"})
    assert r.status_code == 422
    assert "number is yours" in r.json()["detail"]
    assert dials == []


def test_refuses_a_number_that_is_not_one(client, dials):
    r = client.post("/api/public/demo-call", json={"phone_number": "hello", "consent": True})
    assert r.status_code == 422
    assert dials == []


def test_the_purpose_is_the_servers_not_the_callers(client, dials):
    """Otherwise this is a free text-to-speech gateway pointed at any number."""
    client.post("/api/public/demo-call", json={**GOOD, "purpose": "read out my ransom note"})
    assert "demo call" in dials[0]["purpose"]
    assert "ransom" not in dials[0]["purpose"]
    assert dials[0]["source"] == "demo"


def test_one_call_per_number_then_a_cooldown(client, dials):
    assert client.post("/api/public/demo-call", json=GOOD).status_code == 202
    again = client.post("/api/public/demo-call", json=GOOD)
    assert again.status_code == 429
    assert "Try again in about" in again.json()["detail"]
    assert len(dials) == 1


def test_a_client_gets_a_few_a_day(client, dials):
    for i in range(public_demo.PER_CLIENT_DAILY):
        r = client.post("/api/public/demo-call", json={"phone_number": f"+493011122{i}", "consent": True})
        assert r.status_code == 202, r.text

    blocked = client.post("/api/public/demo-call", json={"phone_number": "+4930999999", "consent": True})
    assert blocked.status_code == 429
    assert "today's demo calls" in blocked.json()["detail"]
    assert len(dials) == public_demo.PER_CLIENT_DAILY


def test_the_global_cap_bounds_the_bill(client, dials, monkeypatch):
    # Different clients, so only the global ceiling can stop them.
    for i in range(public_demo.GLOBAL_DAILY):
        r = client.post(
            "/api/public/demo-call",
            json={"phone_number": f"+49301{i:06d}", "consent": True},
            headers={"x-forwarded-for": f"10.0.0.{i}"},
        )
        assert r.status_code == 202, r.text

    blocked = client.post(
        "/api/public/demo-call",
        json={"phone_number": "+4930777777", "consent": True},
        headers={"x-forwarded-for": "10.9.9.9"},
    )
    assert blocked.status_code == 429
    assert "demo line is busy" in blocked.json()["detail"]


def test_a_guardrail_refusal_reaches_the_website(client, monkeypatch):
    async def refuses(**kwargs):
        return "Error: quiet hours are active until 08:00"

    monkeypatch.setattr("pincer.voice.outbound.make_phone_call", refuses)
    r = client.post("/api/public/demo-call", json=GOOD)
    assert r.status_code == 409
    assert "quiet hours" in r.json()["detail"]


def test_a_blocked_attempt_still_costs_the_caller_its_slot(client, monkeypatch):
    """A refusal must not be a free retry loop against the same number."""

    async def refuses(**kwargs):
        return "Error: number is on the do-not-call list"

    monkeypatch.setattr("pincer.voice.outbound.make_phone_call", refuses)
    assert client.post("/api/public/demo-call", json=GOOD).status_code == 409
    assert client.post("/api/public/demo-call", json=GOOD).status_code == 429
