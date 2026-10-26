"""`pincer telephony reclassify`: historical unanswered calls get their real code back."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from support import seeded_id
from typer.testing import CliRunner

from pincer.cli.telephony import _parse_since, _retention_note, telephony_app
from pincer.db import ensure_schema_current
from pincer.db.engine import get_database_url, get_engine
from pincer.models.audit import AuditLog
from pincer.models.telephony import TelephonyCall, TelephonyEvent, TelephonySpan, TelephonyTurn
from pincer.models.voice import VoiceCall
from pincer.services.audit import AuditService
from pincer.services.telemetry import TelemetryService
from pincer.services.voice import CallsService
from pincer.voice.telemetry import reclassify as rc
from pincer.voice.telemetry.schema import EventName

T0 = datetime.now(UTC) - timedelta(hours=2)
SINCE = (T0 - timedelta(hours=1)).isoformat()


# ── derivation rules ─────────────────────────────────────────────────


def _event(name: EventName, **attributes: str) -> dict:
    return {"name": str(name), "ts_utc": T0.isoformat(), "attributes": attributes}


@pytest.mark.parametrize(
    ("status", "code"),
    [("busy", "busy"), ("no-answer", "no_answer"), ("failed", "call_setup"), ("canceled", "call_setup")],
)
def test_not_connected_status_derives_its_code(status, code):
    derived = rc.derive([_event(EventName.PROVIDER_STATUS, status=status)], turn_count=0)
    assert derived is not None and str(derived[0]) == code


def test_the_last_not_connected_status_wins():
    events = [_event(EventName.PROVIDER_STATUS, status=s) for s in ("ringing", "no-answer", "canceled")]
    derived = rc.derive(events, turn_count=0)
    assert derived is not None and str(derived[0]) == "call_setup"


@pytest.mark.parametrize("answered_by", ["machine_end_beep", "machine_start", "fax"])
def test_machine_verdict_without_turns_is_voicemail(answered_by):
    derived = rc.derive([_event(EventName.AMD_VERDICT, answered_by=answered_by)], turn_count=0)
    assert derived is not None and str(derived[0]) == "voicemail"


def test_machine_verdict_with_turns_proves_nothing():
    """Someone spoke: the verdict may have been ignored as a false positive."""
    assert rc.derive([_event(EventName.AMD_VERDICT, answered_by="machine_start")], turn_count=2) is None


def test_human_verdict_and_completed_status_derive_nothing():
    events = [_event(EventName.AMD_VERDICT, answered_by="human"), _event(EventName.PROVIDER_STATUS, status="completed")]
    assert rc.derive(events, turn_count=0) is None


# ── plan / apply through the store, on both dialects ─────────────────

_TABLES = [
    VoiceCall.__table__,
    TelephonyCall.__table__,
    TelephonyEvent.__table__,
    TelephonySpan.__table__,
    TelephonyTurn.__table__,
    AuditLog.__table__,
]

# call_sid → (stored code, provider statuses, AMD verdict, turns, has telemetry)
HISTORY = {
    "CA_busy": ("none", ["ringing", "busy"], "", 0, True),
    "CA_vm_early": ("none", [], "machine_end_beep", 0, True),
    "CA_vm_spoken": ("callee_hangup", [], "machine_start", 1, True),
    "CA_human": ("none", ["in-progress", "completed"], "human", 3, True),
    "CA_no_telemetry": ("none", [], "", 0, False),
    "CA_classified": ("ws_drop", ["busy"], "", 0, True),
    # A person answered and the false-positive verdict was ignored, on a call
    # too unsampled to keep turn rows: only the call row counts its turns.
    "CA_vm_unsampled": ("none", [], "machine_start", 2, True),
}
UNSAMPLED = {"CA_vm_unsampled"}


async def _seed(url: str) -> None:
    """Rows as the old code wrote them: unanswered calls recorded as `none`."""
    calls = CallsService(url)
    telemetry = TelemetryService(url)
    for n, (sid, (code, statuses, amd, turns, traced)) in enumerate(HISTORY.items()):
        started = (T0 + timedelta(minutes=n)).isoformat()
        ended = (T0 + timedelta(minutes=n, seconds=30)).isoformat()
        await calls.save_call(
            {"call_sid": sid, "direction": "outbound", "started_at": started, "ended_at": ended, "failure_code": code}
        )
        if not traced:
            continue
        call_id = seeded_id(sid)
        await telemetry.upsert_call(
            call_id,
            {"provider_call_id": sid, "direction": "outbound", "registered_at": started, "status": "completed"},
        )
        events = [(EventName.PROVIDER_STATUS, {"status": s}) for s in statuses]
        if amd:
            events.append((EventName.AMD_VERDICT, {"answered_by": amd}))
        await telemetry.write_records(
            [
                {
                    "event_id": f"{sid}-{i}",
                    "call_id": call_id,
                    "provider_call_id": sid,
                    "trace_id": "",
                    "span_id": "",
                    "turn_id": "",
                    "name": str(name),
                    "ts_utc": (T0 + timedelta(minutes=n, seconds=i)).isoformat(),
                    "mono_ns": i,
                    "seq": i,
                    "attributes": json.dumps(attributes),
                }
                for i, (name, attributes) in enumerate(events)
            ],
            [],
        )
        # As `TurnTracer._persist`: every turn counts on the call row, only a
        # sampled call keeps the turn row.
        for t in range(turns):
            if sid not in UNSAMPLED:
                await telemetry.upsert_turn(
                    seeded_id(f"{sid}-turn-{t}"), {"call_id": call_id, "turn_no": t + 1, "created_at": started}
                )
            await telemetry.upsert_call(call_id, {"turn_count": 1})
    # An inbound call is never a candidate.
    await calls.save_call(
        {"call_sid": "CA_inbound", "direction": "inbound", "started_at": T0.isoformat(), "failure_code": "none"}
    )


@pytest.fixture
async def url(db_url: str):
    engine = get_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: VoiceCall.metadata.drop_all(c, tables=_TABLES))
        await conn.run_sync(lambda c: VoiceCall.metadata.create_all(c, tables=_TABLES))
    await _seed(db_url)
    yield db_url
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: VoiceCall.metadata.drop_all(c, tables=_TABLES))


async def test_reclassify_dry_run_and_apply(url):
    calls = CallsService(url)

    plan = await rc.plan(url, SINCE)
    assert {c.call_sid: (c.old, c.new) for c in plan.changes} == {
        "CA_busy": ("none", "busy"),
        "CA_vm_early": ("none", "voicemail"),
    }
    assert {sid for sid, _, _ in plan.undetermined} == {
        "CA_vm_spoken",
        "CA_human",
        "CA_no_telemetry",
        "CA_vm_unsampled",
    }
    assert plan.rates["before"]["call_success_rate"] > plan.rates["after"]["call_success_rate"]
    assert (await calls.get("CA_busy") or {})["failure_code"] == "none", "a plan writes nothing"

    await rc.apply(url, plan)

    expected = {
        "CA_busy": "busy",
        "CA_vm_early": "voicemail",
        "CA_vm_spoken": "callee_hangup",
        "CA_human": "none",
        "CA_no_telemetry": "none",
        "CA_classified": "ws_drop",
        "CA_vm_unsampled": "none",
    }
    for sid, code in expected.items():
        assert (await calls.get(sid) or {})["failure_code"] == code, sid
    async with TelemetryService(url).reads() as reads:
        assert reads is not None
        row = await reads.call("CA_busy")
    assert row is not None
    assert (row["status"], row["failure_code"], row["failure_category"], row["termination_reason"]) == (
        "failed",
        "busy",
        "callee_unavailable",
        "busy",
    )

    audit = await AuditService(url).query(action="telephony.reclassify")
    assert len(audit) == 1
    metadata = json.loads(audit[0]["metadata_json"])
    assert metadata["changed"] == 2
    assert {c["call_sid"]: (c["old"], c["new"]) for c in metadata["changes"]} == {
        "CA_busy": ("none", "busy"),
        "CA_vm_early": ("none", "voicemail"),
    }
    assert all(c["evidence"] for c in metadata["changes"])

    again = await rc.plan(url, SINCE)
    assert again.changes == []


# ── the CLI ──────────────────────────────────────────────────────────


_NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("30d", _NOW - timedelta(days=30)),
        ("12h", _NOW - timedelta(hours=12)),
        ("2026-10-01", datetime(2026, 10, 1, tzinfo=UTC)),
        ("2026-10-01T08:30:00+02:00", datetime(2026, 10, 1, 6, 30, tzinfo=UTC)),
    ],
)
def test_parse_since(value, expected):
    assert _parse_since(value, _NOW) == expected


def test_parse_since_normalises_offset_to_utc():
    # SQLite compares started_at as text, so the bound must serialise as +00:00 too.
    start = _parse_since("2026-10-01T00:00:00+02:00", _NOW)
    assert start.isoformat() == "2026-09-30T22:00:00+00:00"
    assert start.isoformat() <= "2026-09-30T22:30:00+00:00"


@pytest.mark.parametrize("value", ["", "30", "d", "30m", "-3d", "yesterday"])
def test_parse_since_rejects(value):
    with pytest.raises(ValueError):
        _parse_since(value, _NOW)


def test_cli_rejects_out_of_range_since():
    result = CliRunner().invoke(telephony_app, ["reclassify", "--since", "1000000d"])
    assert result.exit_code == 2
    assert "--since must look like" in result.output


def test_retention_note_only_beyond_retention():
    assert _retention_note(_NOW - timedelta(days=10), _NOW, 30) == ""
    assert _retention_note(_NOW - timedelta(days=90), _NOW, 0) == ""
    assert "2026-09-23" in _retention_note(_NOW - timedelta(days=90), _NOW, 30)


async def test_cli_dry_run_then_apply(tmp_path, monkeypatch):
    db_path = tmp_path / "pincer.db"
    await asyncio.to_thread(ensure_schema_current, db_path)
    await _seed(get_database_url(Path(db_path)))
    monkeypatch.setattr(
        "pincer.cli.telephony._settings",
        lambda: SimpleNamespace(db_path=str(db_path), telephony_telemetry_retention_days=30),
    )
    runner = CliRunner()

    def invoke(*args: str):
        return asyncio.to_thread(runner.invoke, telephony_app, ["reclassify", "--since", "1d", *args])

    dry = await invoke()
    assert dry.exit_code == 0, dry.output
    assert "CA_busy" in dry.output and "CA_vm_early" in dry.output
    assert "Could not determine" in dry.output
    assert "Dry run: 2" in dry.output

    as_json = await invoke("--json")
    assert {c["call_sid"] for c in json.loads(as_json.output)["changes"]} == {"CA_busy", "CA_vm_early"}

    applied = await invoke("--apply")
    assert "Reclassified 2" in applied.output
    again = await invoke("--apply")
    assert "Nothing to reclassify" in again.output

    old = await asyncio.to_thread(runner.invoke, telephony_app, ["reclassify", "--since", "90d"])
    assert "cannot be corrected" in old.output
