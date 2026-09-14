"""
Telephony API: contract, access boundaries, and what it refuses to expose.

Two things are load-bearing here beyond "the endpoint returns 200":

* **Tenant isolation fails closed.** A caller scoped to tenant A must not see
  tenant B's calls, and a caller scoped to nothing must see nothing — never
  everything.
* **Technical telemetry stays technical.** No transcript, no recording, no tool
  payload, no unmasked phone number may appear in any response, whatever the
  caller asks for.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pincer.api.telephony import router as telephony_router
from pincer.db import ensure_schema_current
from pincer.voice.telemetry import runtime
from pincer.voice.telemetry.recorder import get_recorder
from pincer.voice.telemetry.schema import EventName, SpanName
from pincer.voice.telemetry.tracer import drain_pending

MS = 1_000_000


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """An app wired to a database holding two tenants' calls.

    Seeding runs in its own `asyncio.run` and finishes before any request: the
    TestClient drives its own loop, and sharing one with a live telemetry
    exporter deadlocks rather than testing anything.
    """
    db_path = tmp_path / "telephony.db"
    ensure_schema_current(db_path)

    async def _seed() -> None:
        runtime.reset_for_tests(db_path=str(db_path))
        await get_recorder().start()
        for sid, tenant, engine, failure in (
            ("CA_alpha", "tenant-a", "media_streams", "none"),
            ("CA_beta", "tenant-b", "conversation_relay", "ws_drop"),
        ):
            tracer = runtime.start_call(
                provider_call_id=sid,
                direction="inbound",
                engine=engine,
                language="de",
                tenant_id=tenant,
                from_number="+4915112345678",
                to_number="+493012345678",
            )
            tracer.answered()
            tracer.media_open()
            origin = tracer.watch.started_ns
            turn = tracer.start_turn(speech_end_ns=origin)
            turn.stamp(EventName.LLM_REQUEST, at_ns=origin + 20 * MS)
            turn.stamp(EventName.LLM_FIRST_TOKEN, at_ns=origin + 300 * MS)
            turn.open_span(SpanName.TTS).close()
            turn.first_audio(kind="audio", at_ns=origin + 500 * MS)
            turn.finish()
            await tracer.finish(
                status="completed" if failure == "none" else "failed",
                failure_code=failure,
            )
        await drain_pending()
        await get_recorder().flush()
        # Stop the exporter but keep the counters, which /health reports on.
        await get_recorder().stop(drain_timeout_s=1.0)

    asyncio.run(_seed())

    class _Settings:
        # NOTE: a class body is not a closure, so the path is bound below.
        db_path = None
        telephony_min_samples = 20
        alert_response_latency_p95_ms = 2000.0
        alert_response_latency_window_min = 30
        alert_telephony_window_min = 60
        alert_telephony_min_calls = 10
        alert_connection_rate_min = 0.9
        alert_technical_failure_rate_max = 0.05
        alert_unexpected_disconnect_rate_max = 0.02
        alert_stage_timeout_max = 3
        alert_audio_queue_p95_ms = 250.0
        alert_telemetry_coverage_min = 0.95

    _Settings.db_path = db_path
    monkeypatch.setattr("pincer.api.telephony.get_settings_relaxed", lambda: _Settings())

    app = FastAPI()
    app.include_router(telephony_router)
    yield app
    asyncio.run(runtime.shutdown())
    runtime.reset_for_tests()


def _client(app: FastAPI, tenants: list[str] | None = None) -> TestClient:
    if tenants is not None:

        @app.middleware("http")
        async def _scope(request, call_next):  # type: ignore[no-untyped-def]
            request.state.tenant_ids = tenants
            return await call_next(request)

    return TestClient(app)


# ── contract ─────────────────────────────────────────────────────────


def test_metric_definitions_are_served_with_their_boundaries(seeded):
    body = _client(seeded).get("/api/telephony/metrics").json()
    by_key = {m["key"]: m for m in body}
    assert by_key["response_latency_ms"]["start_event"] == "stt.speech_end"
    assert "SENT, not heard" in by_key["response_latency_ms"]["limitations"]
    assert by_key["caller_perceived_latency_ms"]["source"] == "unavailable"
    assert by_key["caller_perceived_latency_ms"]["unavailable_reason"]


def test_overview_reports_counts_rates_stages_and_coverage(seeded):
    body = _client(seeded).get("/api/telephony/overview?hours=24").json()
    assert body["calls"]["total"] == 2
    assert body["rates"]["connection_rate"]["denominator"] == 2
    assert body["stages"]["response_latency_ms"]["count"] == 2
    assert body["stages"]["response_latency_ms"]["sufficient_samples"] is False
    assert body["coverage"]["calls_with_telemetry"] == 2
    assert body["denominators"]["connection_rate"]
    assert body["telemetry"]["enabled"] is True


def test_call_search_is_filterable_and_paginated(seeded):
    client = _client(seeded)
    everything = client.get("/api/telephony/calls?hours=24").json()
    assert everything["total"] == 2

    filtered = client.get("/api/telephony/calls?hours=24&engine=media_streams").json()
    assert [c["provider_call_id"] for c in filtered["calls"]] == ["CA_alpha"]

    searched = client.get("/api/telephony/calls?hours=24&search=CA_beta").json()
    assert searched["total"] == 1

    paged = client.get("/api/telephony/calls?hours=24&limit=1&offset=1").json()
    assert paged["total"] == 2
    assert len(paged["calls"]) == 1


def test_call_detail_exposes_turns_events_and_spans(seeded):
    client = _client(seeded)
    detail = client.get("/api/telephony/calls/CA_alpha").json()
    assert detail["call"]["provider_call_id"] == "CA_alpha"
    assert len(detail["turns"]) == 1
    assert detail["turns"][0]["response_latency_ms"] == pytest.approx(500.0, abs=2.0)

    events = client.get("/api/telephony/calls/CA_alpha/events").json()
    assert {"call.registered", "call.answered", "turn.start", "call.ended"} <= {e["name"] for e in events}

    spans = client.get("/api/telephony/calls/CA_alpha/spans").json()
    assert any(s["name"] == "tts.synthesis" for s in spans)
    assert all("start_offset_ms" in s for s in spans)


def test_a_call_can_be_reached_by_internal_id_too(seeded):
    client = _client(seeded)
    internal = client.get("/api/telephony/calls/CA_alpha").json()["call"]["call_id"]
    assert client.get(f"/api/telephony/calls/{internal}").status_code == 200


def test_relay_call_lists_what_its_engine_cannot_measure(seeded):
    detail = _client(seeded).get("/api/telephony/calls/CA_beta").json()
    unavailable = {m["key"] for m in detail["unavailable"]}
    assert "tts_first_audio_ms" in unavailable
    assert "endpointing_ms" in unavailable
    for metric in detail["unavailable"]:
        assert metric["unavailable_reason"]


def test_unknown_call_is_a_404(seeded):
    assert _client(seeded).get("/api/telephony/calls/CA_nope").status_code == 404


def test_slowest_turns_carry_their_attributed_bottleneck(seeded):
    rows = _client(seeded).get("/api/telephony/turns/slowest?hours=24").json()
    assert rows
    assert rows[0]["bottleneck_stage"]
    assert rows[0]["provider_call_id"]


def test_alerts_include_quiet_rules_so_absence_is_distinguishable(seeded):
    rows = _client(seeded).get("/api/telephony/alerts").json()
    rules = {row["rule"] for row in rows}
    assert {"response_latency_p95", "connection_rate", "technical_failure_rate", "telemetry_export"} <= rules
    latency = next(r for r in rows if r["rule"] == "response_latency_p95")
    assert latency["firing"] is False
    assert latency["insufficient_data"] is True  # 2 turns < min 20


def test_health_reports_export_counters(seeded):
    body = _client(seeded).get("/api/telephony/health").json()
    assert body["enabled"] is True
    assert body["export"]["exported"] > 0
    assert body["export"]["dropped_queue_full"] == 0


# ── access boundaries ────────────────────────────────────────────────


def test_a_tenant_scoped_caller_sees_only_its_own_calls(seeded):
    client = _client(seeded, tenants=["tenant-a"])
    listing = client.get("/api/telephony/calls?hours=24").json()
    assert [c["provider_call_id"] for c in listing["calls"]] == ["CA_alpha"]
    assert client.get("/api/telephony/calls/CA_beta").status_code == 404
    assert client.get("/api/telephony/calls/CA_alpha").status_code == 200


def test_scoping_applies_to_aggregates_too(seeded):
    body = _client(seeded, tenants=["tenant-b"]).get("/api/telephony/overview?hours=24").json()
    assert body["calls"]["total"] == 1


def test_an_unscoped_caller_cannot_widen_scope_with_a_header(seeded):
    client = _client(seeded, tenants=["tenant-a"])
    response = client.get("/api/telephony/calls?hours=24", headers={"X-Pincer-Tenant": "tenant-b"})
    assert response.status_code == 403


def test_a_caller_with_no_tenants_sees_nothing_not_everything(seeded):
    """Fail closed: an empty permission set is not a wildcard."""
    client = _client(seeded, tenants=[])
    assert client.get("/api/telephony/calls?hours=24").json()["total"] == 0
    assert client.get("/api/telephony/overview?hours=24").json()["calls"]["total"] == 0
    assert client.get("/api/telephony/calls/CA_alpha").status_code == 404


def test_single_tenant_deployment_is_unrestricted(seeded):
    assert _client(seeded).get("/api/telephony/calls?hours=24").json()["total"] == 2


# ── content policy ───────────────────────────────────────────────────


def test_no_response_carries_conversation_content_or_an_unmasked_number(seeded):
    """Technical telemetry stays technical.

    Checked structurally (no content-bearing KEY anywhere in the payload) rather
    than by string search: the metric definitions legitimately use the word
    "transcript" when explaining what ConversationRelay does and does not give us.
    """
    import json

    client = _client(seeded)
    responses = [
        client.get("/api/telephony/overview?hours=24"),
        client.get("/api/telephony/calls?hours=24"),
        client.get("/api/telephony/calls/CA_alpha"),
        client.get("/api/telephony/calls/CA_alpha/events"),
        client.get("/api/telephony/calls/CA_alpha/spans"),
    ]
    forbidden = {
        "transcript",
        "text",
        "utterance",
        "prompt",
        "content",
        "audio",
        "payload",
        "args",
        "arguments",
        "result",
        "recording_url",
        "api_key",
        "authorization",
    }

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                assert key.lower() not in forbidden, f"content-bearing key {key!r} in a telemetry response"
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for response in responses:
        assert "+4915112345678" not in response.text
        walk(json.loads(response.text))


def test_numbers_are_masked_on_the_call_row(seeded):
    call = _client(seeded).get("/api/telephony/calls/CA_alpha").json()["call"]
    assert call["from_number_masked"].startswith("+49")
    assert call["from_number_masked"] != "+4915112345678"
