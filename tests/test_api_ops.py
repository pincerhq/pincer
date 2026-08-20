"""Voice Ops API (Sprint 9) — /api/ops/* and the T9.3 fields on /api/voice/calls."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

os.environ.setdefault("PINCER_ANTHROPIC_API_KEY", "sk-ant-test-key")

from fastapi.testclient import TestClient

from pincer.api.server import create_app
from pincer.observability.call_costs import ensure_call_costs_table
from pincer.voice.retention import ensure_voice_tables


@pytest.fixture
def client(monkeypatch, tmp_path):
    from pincer.config import get_settings_relaxed

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PINCER_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PINCER_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("PINCER_WEB_CHAT_TOKEN", raising=False)
    get_settings_relaxed.cache_clear()
    yield TestClient(create_app())
    get_settings_relaxed.cache_clear()


async def _seed(db_path, rows: list[tuple[str, str, float]]) -> None:
    """rows: (call_sid, failure_code, cost_usd)."""
    started = datetime.now(UTC) - timedelta(minutes=30)
    async with aiosqlite.connect(db_path) as db:
        await ensure_voice_tables(db)
        await ensure_call_costs_table(db)
        for sid, code, cost in rows:
            await db.execute(
                "INSERT INTO voice_calls (call_sid, direction, from_number, to_number, started_at, ended_at, "
                "failure_code, engine, language) VALUES (?, 'outbound', '+4915100000001', '+4915100000002', "
                "?, ?, ?, 'conversation_relay', 'de')",
                (sid, started.isoformat(), (started + timedelta(seconds=60)).isoformat(), code),
            )
            await db.execute(
                "INSERT INTO call_costs (call_sid, total_usd, twilio_usd, llm_usd, recorded_at) VALUES (?, ?, ?, ?, ?)",
                (sid, cost, cost * 0.7, cost * 0.3, datetime.now(UTC).isoformat()),
            )
        await db.commit()


# ── Golden signals ───────────────────────────────────────────────────


def test_signals_endpoint_returns_all_five(client):
    body = client.get("/api/ops/signals").json()
    assert set(body["signals"]) == {
        "call_success_rate",
        "booking_success_rate",
        "turn_latency_p95",
        "stuck_calls",
        "cost_per_call",
        "busy_capacity",  # Sprint 12 §10.3
    }
    assert body["generated_at"]


def test_signals_on_an_empty_system_report_insufficient_not_zero(client):
    signals = client.get("/api/ops/signals").json()["signals"]
    assert signals["call_success_rate"]["value"] is None
    assert signals["call_success_rate"]["sufficient_data"] is False


async def test_signals_reflect_seeded_calls(client, tmp_path):
    await _seed(tmp_path / "pincer.db", [(f"CA{i}", "none" if i < 4 else "ws_drop", 0.2) for i in range(6)])
    signals = client.get("/api/ops/signals").json()["signals"]
    assert signals["call_success_rate"]["value"] == pytest.approx(4 / 6)
    assert signals["call_success_rate"]["detail"]["by_failure_code"]["ws_drop"] == 2


# ── Alerts ───────────────────────────────────────────────────────────


def test_alerts_endpoint_is_quiet_on_a_healthy_system(client):
    assert client.get("/api/ops/alerts").json() == []


async def test_alerts_endpoint_does_not_deliver(client, tmp_path, monkeypatch):
    """Opening the dashboard must never notify anyone."""
    delivered: list[str] = []

    async def _notifier(user_id, channel, text):
        delivered.append(text)
        return True

    from pincer.voice import status_notify

    status_notify.set_status_notifier(_notifier)
    try:
        await _seed(tmp_path / "pincer.db", [(f"CA{i}", "ws_drop", 0.2) for i in range(10)])
        response = client.get("/api/ops/alerts")
        assert response.status_code == 200
        assert any(a["rule"] == "call_success_rate" for a in response.json())
        assert delivered == []
    finally:
        status_notify.set_status_notifier(None)


async def test_alert_carries_its_runbook_anchor(client, tmp_path):
    await _seed(tmp_path / "pincer.db", [(f"CA{i}", "ws_drop", 0.2) for i in range(10)])
    alerts = client.get("/api/ops/alerts").json()
    assert alerts[0]["runbook"].startswith("docs/operations/runbook.md#")


# ── SLO ──────────────────────────────────────────────────────────────


def test_slo_endpoint_shape(client):
    body = client.get("/api/ops/slo").json()
    assert {s["name"] for s in body["slos"]} == {
        "call_attempt_success",
        "turn_latency_p95",
        "report_delivery",
        "availability",
    }
    assert body["freeze_threshold_pct"] == pytest.approx(50.0)
    assert body["freeze_min_sample"] >= 1


def test_availability_is_labelled_inferred(client):
    """We only know the service was up when the canary ran — say so."""
    body = client.get("/api/ops/slo").json()
    availability = next(s for s in body["slos"] if s["name"] == "availability")
    assert availability["confidence"] == "inferred"


def test_empty_system_declares_no_freeze(client):
    assert client.get("/api/ops/slo").json()["feature_freeze"] is False


# ── Failures ─────────────────────────────────────────────────────────


async def test_failures_endpoint_ranks_codes_with_descriptions(client, tmp_path):
    await _seed(
        tmp_path / "pincer.db",
        [("CA1", "ws_drop", 0.2), ("CA2", "ws_drop", 0.2), ("CA3", "no_answer", 0.1), ("CA4", "none", 0.2)],
    )
    body = client.get("/api/ops/failures?hours=24").json()
    assert body["total"] == 4
    assert body["codes"][0]["code"] == "ws_drop"
    assert body["codes"][0]["count"] == 2
    assert "WebSocket" in body["codes"][0]["description"]


# ── Canary ───────────────────────────────────────────────────────────


def test_canary_history_is_empty_initially(client):
    assert client.get("/api/ops/canary").json() == []


def test_canary_trigger_refuses_when_disabled(client):
    """A dashboard button must not be able to dial an unconfigured number."""
    response = client.post("/api/ops/canary")
    assert response.status_code == 409
    assert "disabled" in response.json()["detail"].lower()


# ── Digest ───────────────────────────────────────────────────────────


def test_digest_endpoint_renders_without_sending(client):
    body = client.get("/api/ops/digest").json()
    assert "Voice weekly digest" in body["digest"]


# ── T9.3 fields on the voice API ─────────────────────────────────────


async def test_call_list_exposes_failure_code_and_cost(client, tmp_path):
    await _seed(tmp_path / "pincer.db", [("CA_ok", "none", 0.25), ("CA_bad", "tts_error", 0.40)])
    calls = {c["call_sid"]: c for c in client.get("/api/voice/calls").json()}

    assert calls["CA_ok"]["failure_code"] == "none"
    assert calls["CA_bad"]["failure_code"] == "tts_error"
    assert "synthesis" in calls["CA_bad"]["failure_description"].lower()
    assert calls["CA_bad"]["cost_usd"] == pytest.approx(0.40)


async def test_call_detail_exposes_the_cost_breakdown(client, tmp_path):
    await _seed(tmp_path / "pincer.db", [("CA_detail", "none", 0.50)])
    detail = client.get("/api/voice/calls/CA_detail").json()
    assert detail["cost"]["total_usd"] == pytest.approx(0.50)
    assert detail["cost"]["twilio_usd"] == pytest.approx(0.35)
    assert detail["cost"]["llm_usd"] == pytest.approx(0.15)


async def test_call_without_a_cost_record_reports_none(client, tmp_path):
    started = datetime.now(UTC) - timedelta(minutes=5)
    async with aiosqlite.connect(tmp_path / "pincer.db") as db:
        await ensure_voice_tables(db)
        await db.execute(
            "INSERT INTO voice_calls (call_sid, direction, started_at, ended_at, failure_code) "
            "VALUES ('CA_nocost', 'inbound', ?, ?, 'none')",
            (started.isoformat(), started.isoformat()),
        )
        await db.commit()

    call = client.get("/api/voice/calls").json()[0]
    assert call["cost_usd"] is None


def test_ops_endpoints_require_auth(monkeypatch, tmp_path):
    """/api/ops/* is behind the same bearer gate as the rest of /api/*."""
    from pincer.config import get_settings_relaxed

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PINCER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PINCER_DASHBOARD_TOKEN", "t" * 40)
    get_settings_relaxed.cache_clear()
    try:
        client = TestClient(create_app())
        assert client.get("/api/ops/signals").status_code == 401
        assert client.get("/api/ops/signals", headers={"Authorization": f"Bearer {'t' * 40}"}).status_code == 200
    finally:
        get_settings_relaxed.cache_clear()


# ── GA gate endpoint (Sprint 10, T10.4) ──────────────────────────────


def test_ga_gate_endpoint_shape(client, monkeypatch):
    """The doctor criterion probes voice providers, so stub it for the test."""
    from unittest.mock import MagicMock

    report = MagicMock()
    report.checks = []
    report.score = 100
    monkeypatch.setattr("pincer.security.doctor.SecurityDoctor.run_all", lambda self: report)

    body = client.get("/api/ops/ga-gate?days=14").json()
    assert body["ready"] is False  # an empty system is never ready
    assert body["summary"]["total"] == len(body["criteria"])
    keys = {c["key"] for c in body["criteria"]}
    assert {"call_volume", "call_success_rate", "latency", "compliance", "cost_per_call"} <= keys


def test_ga_gate_never_passes_on_an_empty_system(client, monkeypatch):
    """The failure mode that matters: 'no data' reading as 'fine'."""
    from unittest.mock import MagicMock

    report = MagicMock()
    report.checks = []
    report.score = 100
    monkeypatch.setattr("pincer.security.doctor.SecurityDoctor.run_all", lambda self: report)

    body = client.get("/api/ops/ga-gate").json()
    volume = next(c for c in body["criteria"] if c["key"] == "call_volume")
    assert volume["verdict"] == "insufficient_data"
    assert volume["needed"]


def test_ga_gate_markdown_report(client, monkeypatch):
    from unittest.mock import MagicMock

    report = MagicMock()
    report.checks = []
    report.score = 100
    monkeypatch.setattr("pincer.security.doctor.SecurityDoctor.run_all", lambda self: report)

    body = client.get("/api/ops/ga-gate/report").json()
    assert "# GA Gate Review" in body["report"]
    assert "NOT READY" in body["report"]
