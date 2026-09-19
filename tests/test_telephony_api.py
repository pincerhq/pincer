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


# ── export ───────────────────────────────────────────────────────────


def test_csv_export_covers_the_whole_filter_not_just_the_page(seeded):
    client = _client(seeded)
    response = client.get("/api/telephony/export?dataset=calls&format=csv&hours=24&limit=1")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["X-Export-Truncated"] == "false"
    assert int(response.headers["X-Export-Rows"]) == 2

    lines = response.text.strip().splitlines()
    assert lines[0].startswith("call_id,provider_call_id,trace_id")
    assert len(lines) == 3  # header + both calls, despite the page-size hint
    assert "CA_alpha" in response.text and "CA_beta" in response.text


def test_export_respects_the_active_filters(seeded):
    response = _client(seeded).get("/api/telephony/export?dataset=calls&format=csv&hours=24&engine=media_streams")
    assert "CA_alpha" in response.text
    assert "CA_beta" not in response.text


def test_export_is_tenant_scoped_like_every_other_read(seeded):
    response = _client(seeded, tenants=["tenant-a"]).get("/api/telephony/export?dataset=calls&format=csv&hours=24")
    assert "CA_alpha" in response.text
    assert "CA_beta" not in response.text


def test_turn_export_is_flat_and_drops_the_nested_path(seeded):
    """A JSON blob in a CSV cell helps nobody."""
    response = _client(seeded).get("/api/telephony/export?dataset=turns&format=csv&hours=24")
    header = response.text.splitlines()[0]
    assert "response_latency_ms" in header
    assert "bottleneck_stage" in header
    assert "critical_path" not in header


def test_json_export_declares_whether_it_was_capped(seeded):
    payload = _client(seeded).get("/api/telephony/export?dataset=stages&format=json&hours=24").json()
    assert payload["dataset"] == "stages"
    assert payload["truncated"] is False
    stages = {row["stage"] for row in payload["rows"]}
    assert "response_latency_ms" in stages
    assert all("samples" in row for row in payload["rows"])


def test_export_never_carries_conversation_content(seeded):
    """The column list is the review point for what leaves the system."""
    from pincer.api.telephony import EXPORT_COLUMNS

    forbidden = {"text", "transcript", "utterance", "prompt", "content", "audio", "args", "result", "recording_url"}
    for dataset, columns in EXPORT_COLUMNS.items():
        assert not (set(columns) & forbidden), dataset
        if dataset == "calls":
            assert "from_number_masked" in columns
            assert "from_number" not in columns

    body = _client(seeded).get("/api/telephony/export?dataset=calls&format=csv&hours=24").text
    assert "+4915112345678" not in body


def test_an_unknown_dataset_is_rejected(seeded):
    assert _client(seeded).get("/api/telephony/export?dataset=secrets&format=csv").status_code == 422


# ── overall statistics ───────────────────────────────────────────────


def test_overview_exports_every_leaf_of_the_aggregate(seeded):
    """The flattened table must not quietly drop a section the JSON has."""
    response = _client(seeded).get("/api/telephony/export?dataset=overview&format=csv&hours=24")
    assert response.status_code == 200
    assert response.text.splitlines()[0] == "section,key,metric,value"

    rows = [line.split(",") for line in response.text.strip().splitlines()[1:]]
    sections = {row[0] for row in rows}
    assert {"calls", "rates", "stages", "reliability", "coverage", "denominators"} <= sections

    # A rate keeps the numbers that make it interpretable, not just its value.
    flat = {(row[0], row[1], row[2]) for row in rows}
    assert ("rates", "connection_rate", "value") in flat
    assert ("rates", "connection_rate", "denominator") in flat


def test_overview_json_keeps_the_nesting_the_csv_has_to_flatten(seeded):
    payload = _client(seeded).get("/api/telephony/export?dataset=overview&format=json&hours=24").json()
    assert payload["dataset"] == "overview"
    assert payload["window_hours"] == 24
    assert payload["calls"]["total"] == 2
    assert "connection_rate" in payload["rates"]


def test_the_archive_carries_the_window_and_the_means_to_read_it(seeded):
    import io
    import json
    import zipfile

    response = _client(seeded).get("/api/telephony/export/archive?hours=24")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["X-Export-Truncated"] == "false"

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert set(archive.namelist()) == {
        "README.txt",
        "filters.json",
        "overview.json",
        "overview.csv",
        "stages.csv",
        "calls.csv",
        "turns.csv",
        "metrics.json",
    }

    calls = archive.read("calls.csv").decode()
    assert "CA_alpha" in calls and "CA_beta" in calls

    # The definitions travel with the numbers: an archive read months later must
    # still say what a metric was measured between.
    metrics = json.loads(archive.read("metrics.json"))
    response_latency = next(m for m in metrics if m["key"] == "response_latency_ms")
    assert response_latency["start_event"] and response_latency["end_event"]
    assert response_latency["limitations"]

    readme = archive.read("README.txt").decode()
    assert "Never sum span durations" in readme
    assert "complete set" in readme


def test_the_archive_is_filtered_and_tenant_scoped_like_every_other_read(seeded):
    import io
    import zipfile

    response = _client(seeded, tenants=["tenant-a"]).get("/api/telephony/export/archive?hours=24")
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    calls = archive.read("calls.csv").decode()
    assert "CA_alpha" in calls
    assert "CA_beta" not in calls

    filtered = _client(seeded).get("/api/telephony/export/archive?hours=24&engine=media_streams")
    calls = zipfile.ZipFile(io.BytesIO(filtered.content)).read("calls.csv").decode()
    assert "CA_alpha" in calls
    assert "CA_beta" not in calls


# ── one call ─────────────────────────────────────────────────────────


def test_a_call_exports_complete_from_the_database(seeded):
    """Not from whatever the open page had fetched."""
    import json

    payload = _client(seeded).get("/api/telephony/calls/CA_alpha/export?format=json").json()
    assert payload["call"]["provider_call_id"] == "CA_alpha"
    assert payload["turns"] and payload["events"] and payload["spans"]
    assert payload["truncated"] is False
    # The engine's own limits ride along, so a missing metric reads as a known
    # limitation rather than as a gap in the export.
    assert {m["key"] for m in payload["unavailable"]}
    assert json.dumps(payload, default=str)


def test_a_call_archive_holds_the_timeline_as_csv(seeded):
    import io
    import zipfile

    response = _client(seeded).get("/api/telephony/calls/CA_alpha/export")
    assert response.headers["content-type"] == "application/zip"
    assert "telephony-call-CA_alpha.zip" in response.headers["content-disposition"]

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert set(archive.namelist()) == {
        "README.txt",
        "call.json",
        "turns.csv",
        "events.csv",
        "spans.csv",
        "metrics.json",
    }
    events = archive.read("events.csv").decode()
    assert events.splitlines()[0] == "ts_utc,seq,name,event_id,turn_id,span_id"
    assert "call.answered" in events
    assert "CA_alpha" in archive.read("README.txt").decode()


def test_a_download_name_survives_a_call_that_twilio_never_named(seeded):
    """Outbound calls carry `pending:<uuid>`; a colon is not a filename."""
    from pincer.api.telephony import _safe_filename

    assert _safe_filename("pending:f373eac4-99") == "pending-f373eac4-99"
    assert _safe_filename("CA_alpha") == "CA_alpha"
    assert _safe_filename("///") == "call"


def test_a_call_export_is_tenant_scoped(seeded):
    """Downloading a call is a read, and reads fail closed."""
    assert _client(seeded, tenants=["tenant-a"]).get("/api/telephony/calls/CA_beta/export").status_code == 404


def test_a_call_export_refuses_a_caller_scoped_to_nothing(seeded):
    """Scoped to nothing means nothing — never everything."""
    assert _client(seeded, tenants=[]).get("/api/telephony/calls/CA_alpha/export").status_code == 404


def test_no_export_surface_carries_conversation_content(seeded):
    """Every new download goes through the same review as the old ones.

    The unmasked number is the probe: it is the one piece of real caller data
    the seeded calls were given, and it exists nowhere in these tables.
    """
    import io
    import json
    import zipfile

    client = _client(seeded)
    bodies = {
        "call-json": client.get("/api/telephony/calls/CA_alpha/export?format=json").text,
        "overview-csv": client.get("/api/telephony/export?dataset=overview&format=csv&hours=24").text,
    }
    for label, url in (
        ("window", "/api/telephony/export/archive?hours=24"),
        ("call", "/api/telephony/calls/CA_alpha/export"),
    ):
        archive = zipfile.ZipFile(io.BytesIO(client.get(url).content))
        for name in archive.namelist():
            bodies[f"{label}:{name}"] = archive.read(name).decode()

    for label, body in bodies.items():
        assert "+4915112345678" not in body, label

    # Payload-bearing fields are checked structurally rather than by substring:
    # the metric definitions travel inside these files and their prose *mentions*
    # transcripts and utterances precisely to say they are not measured here.
    forbidden = {"text", "transcript", "utterance", "prompt", "content", "audio", "args", "result", "recording_url"}
    bundle = json.loads(bodies["call-json"])
    fields = set(bundle["call"])
    for section in ("turns", "events", "spans"):
        for row in bundle[section]:
            fields |= set(row)
            fields |= set(row.get("attributes", {}))
    assert not (fields & forbidden), sorted(fields & forbidden)

    for label, body in bodies.items():
        if not label.endswith(".csv") and label != "overview-csv":
            continue
        header = set(body.splitlines()[0].split(","))
        assert not (header & forbidden), f"{label}: {sorted(header & forbidden)}"
