"""Call threads (Sprint 13) — data model, lifecycle, linking, merge, context.

The spec's §12 unit list, plus the boundaries that carry security weight:
the inbound acknowledgment must never leak thread content, and a commitment
is only "done" with evidence in the call that supposedly satisfied it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import aiosqlite
import pytest

from pincer.voice import threads as th
from pincer.voice.outcome import CallOutcome
from pincer.voice.threads import (
    KIND_FOLLOWUP,
    KIND_MANUAL,
    KIND_ORIGIN,
    STATUS_CLOSED,
    STATUS_OPEN,
    STATUS_RESOLVED,
    ThreadError,
    ThreadManager,
)


def _settings(tmp_path, **overrides):
    base = {
        "db_path": str(tmp_path / "pincer.db"),
        "voice_default_language": "de",
        "thread_inbound_context": "off",
        "thread_match_window_days": 7,
        "thread_autoclose_days": 30,
        "dashboard_url": "",
        "default_user_id": "u1",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def manager(tmp_path):
    return ThreadManager(str(tmp_path / "pincer.db"), settings=_settings(tmp_path))


async def _seed_call(db_path: str, call_sid: str, started_at: datetime, direction: str = "outbound") -> None:
    """A voice_calls row as the post-call pipeline would write it."""
    from pincer.voice.retention import ensure_voice_tables

    async with aiosqlite.connect(db_path) as db:
        await ensure_voice_tables(db)
        await db.execute(
            "INSERT OR REPLACE INTO voice_calls (call_sid, direction, from_number, to_number, started_at, ended_at) "
            "VALUES (?, ?, '+4930111', '+4930222', ?, ?)",
            (call_sid, direction, started_at.isoformat(), (started_at + timedelta(minutes=2)).isoformat()),
        )
        await db.commit()


def _outcome(**kwargs) -> CallOutcome:
    data = {
        "outcome": "completed",
        "task_result": "Der Termin am Dienstag wurde bestätigt.",
        "key_facts": [],
        "commitments": [],
        "language": "de",
    }
    data.update(kwargs)
    return CallOutcome(**data)


class FakeLLM:
    """Returns one canned merge response (or blows up, for the fallback path)."""

    def __init__(self, payload=None, raises: bool = False) -> None:
        self._payload = payload
        self._raises = raises
        self.calls = 0

    async def complete(self, messages, tools=None, model=None, max_tokens=None, temperature=None, system=None):
        self.calls += 1
        if self._raises:
            raise RuntimeError("LLM down")
        content = self._payload if isinstance(self._payload, str) else json.dumps(self._payload, ensure_ascii=False)
        return SimpleNamespace(content=content)


# ── Creation & the single-thread rule (§2/§4) ────────────────────────


async def test_thread_create_on_task_call(manager, tmp_path):
    """A new outbound task call opens a thread and attaches as `origin`."""
    thread = await manager.create("Termin Dr. Müller", primary_number="+4930222", contact_name="Dr. Müller")
    assert thread.thread_id.startswith("thr_")
    assert thread.status == STATUS_OPEN
    assert thread.origin == "user_task"

    await _seed_call(manager.db_path, "CA1", datetime.now(UTC))
    await manager.attach("CA1", thread.thread_id, KIND_ORIGIN)

    assert await manager.thread_for_call("CA1") == thread.thread_id
    calls = await manager.calls(thread.thread_id)
    assert [(c.call_sid, c.attach_kind, c.purged) for c in calls] == [("CA1", KIND_ORIGIN, False)]

    # The link is mirrored onto the call row for the /calls API surface.
    async with aiosqlite.connect(manager.db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT thread_id, thread_attach_kind FROM voice_calls WHERE call_sid = 'CA1'")
        row = await cursor.fetchone()
    assert row["thread_id"] == thread.thread_id
    assert row["thread_attach_kind"] == KIND_ORIGIN


async def test_thread_subject_is_required_and_capped(manager):
    with pytest.raises(ThreadError):
        await manager.create("   ")
    long_thread = await manager.create("x" * 300)
    assert len(long_thread.subject) <= th.MAX_SUBJECT


async def test_single_thread_rule(manager):
    """A call belongs to at most one thread; only `manual` moves it."""
    first = await manager.create("Matter A")
    second = await manager.create("Matter B")
    await manager.attach("CA1", first.thread_id, KIND_ORIGIN)

    with pytest.raises(ThreadError, match="already belongs to thread"):
        await manager.attach("CA1", second.thread_id, KIND_FOLLOWUP)
    assert await manager.thread_for_call("CA1") == first.thread_id

    # The manual reassignment path is the only detach there is.
    await manager.attach("CA1", second.thread_id, KIND_MANUAL)
    assert await manager.thread_for_call("CA1") == second.thread_id
    assert [c.call_sid for c in await manager.calls(first.thread_id)] == []


async def test_attach_rejects_unknown_kind_and_thread(manager):
    thread = await manager.create("Matter")
    with pytest.raises(ThreadError, match="Unknown attach kind"):
        await manager.attach("CA1", thread.thread_id, "teleport")
    with pytest.raises(ThreadError, match="does not exist"):
        await manager.attach("CA1", "thr_nope", KIND_ORIGIN)


async def test_closed_thread_rejects_attach(manager):
    """§4.2: closed is final — a follow-up must start a new thread."""
    thread = await manager.create("Matter")
    await manager.close(thread.thread_id, reason="test")
    with pytest.raises(ThreadError, match="closed"):
        await manager.attach("CA9", thread.thread_id, KIND_FOLLOWUP)


async def test_followup_param_attach_reopens_resolved(manager):
    """§5: a new call on a resolved thread reopens it."""
    thread = await manager.create("Matter")
    await manager.attach("CA1", thread.thread_id, KIND_ORIGIN)
    await manager.resolve(thread.thread_id, reason="done")
    assert (await manager.require(thread.thread_id)).status == STATUS_RESOLVED

    await manager.attach("CA2", thread.thread_id, KIND_FOLLOWUP)
    reopened = await manager.require(thread.thread_id)
    assert reopened.status == STATUS_OPEN
    assert reopened.resolved_at is None
    assert [c.attach_kind for c in await manager.calls(thread.thread_id)] == [KIND_ORIGIN, KIND_FOLLOWUP]


# ── Lifecycle (§5) ───────────────────────────────────────────────────


async def test_lifecycle_transitions_valid_only(manager):
    thread = await manager.create("Matter")
    tid = thread.thread_id

    await manager.resolve(tid)
    assert (await manager.require(tid)).status == STATUS_RESOLVED
    await manager.reopen(tid)
    assert (await manager.require(tid)).status == STATUS_OPEN
    await manager.close(tid)
    closed = await manager.require(tid)
    assert closed.status == STATUS_CLOSED and closed.closed_at

    # Closed is final in every direction.
    for target in (STATUS_OPEN, STATUS_RESOLVED):
        with pytest.raises(ThreadError, match="closed is final"):
            await manager.set_status(tid, target)

    with pytest.raises(ThreadError, match="Unknown thread status"):
        await manager.set_status(tid, "archived")


async def test_autoclose_only_touches_inactive_threads(manager):
    stale = await manager.create("Old matter")
    fresh = await manager.create("New matter")
    long_ago = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute("UPDATE call_threads SET updated_at = ? WHERE thread_id = ?", (long_ago, stale.thread_id))
        await db.commit()

    assert await manager.autoclose(30) == [stale.thread_id]
    assert (await manager.require(stale.thread_id)).status == STATUS_CLOSED
    assert (await manager.require(fresh.thread_id)).status == STATUS_OPEN
    # 0 = never
    assert await manager.autoclose(0) == []


async def test_merge_moves_calls_and_closes_source(manager):
    source = await manager.create("Duplicate")
    target = await manager.create("Real matter")
    await manager.attach("CA1", source.thread_id, KIND_ORIGIN)
    await manager.attach("CA2", target.thread_id, KIND_ORIGIN)

    merged = await manager.merge(source.thread_id, target.thread_id)
    assert merged.thread_id == target.thread_id
    assert (await manager.require(source.thread_id)).status == STATUS_CLOSED
    assert sorted(c.call_sid for c in await manager.calls(target.thread_id)) == ["CA1", "CA2"]
    assert (await manager.thread_for_call("CA1")) == target.thread_id

    with pytest.raises(ThreadError, match="into itself"):
        await manager.merge(target.thread_id, target.thread_id)


# ── Inbound matching (§4.3) ──────────────────────────────────────────


async def test_inbound_match_exactly_one(manager):
    thread = await manager.create("Rückruf Praxis", primary_number="+4930222")
    found = await manager.find_open_by_number("+4930222", within_days=7)
    assert found is not None and found.thread_id == thread.thread_id


async def test_inbound_match_ambiguous_no_attach(manager):
    """Two open threads on one number is ambiguity — and ambiguity never guesses."""
    await manager.create("Matter A", primary_number="+4930222")
    await manager.create("Matter B", primary_number="+4930222")
    assert await manager.find_open_by_number("+4930222", within_days=7) is None


async def test_inbound_match_respects_status_and_window(manager):
    resolved = await manager.create("Matter", primary_number="+4930222")
    await manager.resolve(resolved.thread_id)
    assert await manager.find_open_by_number("+4930222", within_days=7) is None

    old = await manager.create("Old matter", primary_number="+4930333")
    async with aiosqlite.connect(manager.db_path) as db:
        await db.execute(
            "UPDATE call_threads SET updated_at = ? WHERE thread_id = ?",
            ((datetime.now(UTC) - timedelta(days=30)).isoformat(), old.thread_id),
        )
        await db.commit()
    assert await manager.find_open_by_number("+4930333", within_days=7) is None
    assert await manager.find_open_by_number("+4930333", within_days=60) is not None
    # 0 days = matching disabled outright
    assert await manager.find_open_by_number("+4930333", within_days=0) is None


# ── Rolling summary & commitments (§6) ───────────────────────────────


async def test_summary_merge_cap_and_fallback(manager, tmp_path):
    thread = await manager.create("Termin", language="de")
    await manager.attach("CA1", thread.thread_id, KIND_ORIGIN)

    # An over-long LLM summary is truncated to the §6.1 cap, never stored raw.
    verbose = FakeLLM({"summary": "A" * 5000, "satisfied_commitments": []})
    update = await manager.update_after_call("CA1", outcome=_outcome(), llm=verbose, language="de")
    assert update is not None
    assert len(update.thread.rolling_summary) <= th.MAX_SUMMARY
    assert update.summary_failed is False

    # A failing merge NEVER loses the previous summary — it appends a state line.
    kept = update.thread.rolling_summary
    await manager.attach("CA2", thread.thread_id, KIND_FOLLOWUP)
    broken = await manager.update_after_call(
        "CA2", outcome=_outcome(task_result="Praxis meldet sich Freitag."), llm=FakeLLM(raises=True), language="de"
    )
    assert broken is not None and broken.summary_failed is True
    assert broken.thread.rolling_summary.startswith(kept[:50])
    assert "Stand: Praxis meldet sich Freitag." in broken.thread.rolling_summary


async def test_summary_fallback_without_llm(manager):
    thread = await manager.create("Termin", language="en")
    await manager.attach("CA1", thread.thread_id, KIND_ORIGIN)
    update = await manager.update_after_call("CA1", outcome=_outcome(language="en"), llm=None, language="en")
    assert update is not None
    assert update.summary_failed is True
    assert update.thread.rolling_summary.startswith("Status: ")


async def test_commitment_done_requires_evidence(manager):
    """§6.2: the LLM proposes "satisfied"; only evidence in the NEW call accepts it."""
    thread = await manager.create("Termin", language="de")
    await manager.attach("CA1", thread.thread_id, KIND_ORIGIN)
    first = _outcome(
        outcome="callback_requested",
        commitments=[{"who": "callee", "what": "Die Praxis ruft wegen der Terminbestätigung zurück", "when": None}],
    )
    llm = FakeLLM({"summary": "Erster Anruf.\nStand: wartet auf Rückruf.", "satisfied_commitments": []})
    await manager.update_after_call("CA1", outcome=first, llm=llm, language="de")
    thread = await manager.require(thread.thread_id)
    assert [c["status"] for c in thread.open_commitments] == ["open"]

    # The model claims index 0 is done, but the new call says nothing about it.
    await manager.attach("CA2", thread.thread_id, KIND_FOLLOWUP)
    unrelated = _outcome(task_result="Über das Wetter gesprochen.", key_facts=[])
    claiming = FakeLLM({"summary": "Zweiter Anruf.\nStand: offen.", "satisfied_commitments": [0]})
    await manager.update_after_call("CA2", outcome=unrelated, llm=claiming, language="de")
    assert [c["status"] for c in (await manager.require(thread.thread_id)).open_commitments] == ["open"]

    # Now the new call actually reports the callback happening.
    await manager.attach("CA3", thread.thread_id, KIND_FOLLOWUP)
    evidenced = _outcome(
        task_result="Die Praxis hat wegen der Terminbestätigung zurückgerufen.",
        key_facts=["Die Praxis ruft wegen der Terminbestätigung zurück — erledigt."],
    )
    await manager.update_after_call(
        "CA3",
        outcome=evidenced,
        llm=FakeLLM({"summary": "Dritter Anruf.\nStand: erledigt.", "satisfied_commitments": [0]}),
        language="de",
    )
    assert [c["status"] for c in (await manager.require(thread.thread_id)).open_commitments] == ["done"]


async def test_expired_commitment_is_flagged_not_acted_on(manager):
    past = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    existing = [{"who": "callee", "what": "ruft zurück", "due": past, "status": "open", "source_call_sid": "CA1"}]
    merged, changed = th.merge_commitments(existing, _outcome(), [], "CA2")
    assert changed is True
    assert merged[0]["status"] == "expired"


async def test_update_after_call_resolves_on_terminal_success(manager):
    thread = await manager.create("Termin", language="de")
    await manager.attach("CA1", thread.thread_id, KIND_ORIGIN)
    llm = FakeLLM({"summary": "Termin bestätigt.\nStand: erledigt.", "satisfied_commitments": []})
    update = await manager.update_after_call("CA1", outcome=_outcome(outcome="completed"), llm=llm, language="de")
    assert update is not None and update.resolved_now is True
    assert (await manager.require(thread.thread_id)).status == STATUS_RESOLVED


async def test_update_after_call_keeps_open_while_commitments_remain(manager):
    thread = await manager.create("Termin", language="de")
    await manager.attach("CA1", thread.thread_id, KIND_ORIGIN)
    outcome = _outcome(commitments=[{"who": "callee", "what": "schickt die Unterlagen", "when": None}])
    llm = FakeLLM({"summary": "Läuft.\nStand: offen.", "satisfied_commitments": []})
    update = await manager.update_after_call("CA1", outcome=outcome, llm=llm, language="de")
    assert update is not None and update.resolved_now is False
    assert (await manager.require(thread.thread_id)).status == STATUS_OPEN


async def test_update_after_call_is_noop_for_threadless_calls(manager):
    assert await manager.update_after_call("CA_unknown", outcome=_outcome(), llm=FakeLLM({})) is None


# ── Retention (§5) ───────────────────────────────────────────────────


async def test_purged_call_stub_survives(manager, tmp_path):
    """The transcript purge takes the call row; the thread keeps the stub."""
    from pincer.voice.retention import purge_expired_voice_data

    thread = await manager.create("Termin", language="de")
    long_ago = datetime.now(UTC) - timedelta(days=200)
    await _seed_call(manager.db_path, "CA_old", long_ago)
    await manager.attach("CA_old", thread.thread_id, KIND_ORIGIN)
    await manager.update_after_call(
        "CA_old",
        outcome=_outcome(outcome="voicemail", task_result="Anrufbeantworter erreicht."),
        llm=FakeLLM({"summary": "Nicht erreicht.\nStand: offen.", "satisfied_commitments": []}),
        language="de",
    )

    deleted = await purge_expired_voice_data(manager.db_path, retention_days=90)
    assert deleted.get("voice_calls") == 1

    surviving = await manager.require(thread.thread_id)
    assert surviving.rolling_summary  # derived facts outlive the transcript
    stubs = await manager.calls(thread.thread_id)
    assert len(stubs) == 1
    assert stubs[0].purged is True
    assert stubs[0].call_sid == "CA_old"
    assert stubs[0].outcome_code == "voicemail"


# ── Context injection (§7) & the inbound boundary (§4.3) ─────────────


async def test_context_block_size_bound(manager, tmp_path):
    thread = await manager.create("Termin Dr. Müller", language="de", primary_number="+4930222")
    await _seed_call(manager.db_path, "CA1", datetime.now(UTC) - timedelta(days=2))
    await manager.attach("CA1", thread.thread_id, KIND_ORIGIN)
    await manager.update_after_call(
        "CA1",
        outcome=_outcome(
            outcome="partial",
            task_result="X" * 400,
            commitments=[{"who": "callee", "what": f"Zusage {i} " + "y" * 100, "when": None} for i in range(6)],
        ),
        llm=FakeLLM({"summary": "Z" * 4000, "satisfied_commitments": []}),
        language="de",
    )
    block = await manager.build_context(thread.thread_id, "outbound", _settings(tmp_path))
    assert block
    assert len(block) <= th.MAX_CONTEXT_BLOCK
    assert "THREAD-KONTEXT" in block


async def test_first_call_of_thread_has_no_context_block(manager, tmp_path):
    """No summary yet = no empty scaffolding in the prompt."""
    thread = await manager.create("Termin", language="de")
    assert await manager.build_context(thread.thread_id, "outbound", _settings(tmp_path)) == ""


async def test_context_block_carries_summary_and_last_call(manager, tmp_path):
    thread = await manager.create("Termin Dr. Müller", language="de")
    await _seed_call(manager.db_path, "CA1", datetime(2026, 8, 18, 10, 0, tzinfo=UTC))
    await manager.attach("CA1", thread.thread_id, KIND_ORIGIN)
    await manager.update_after_call(
        "CA1",
        outcome=_outcome(
            outcome="callback_requested",
            task_result="Praxis meldet sich.",
            commitments=[{"who": "callee", "what": "ruft am Freitag zurück", "when": None}],
        ),
        llm=FakeLLM({"summary": "Dienstag angerufen.\nStand: wartet auf Rückruf.", "satisfied_commitments": []}),
        language="de",
    )
    block = await manager.build_context(thread.thread_id, "outbound", _settings(tmp_path))
    assert "Dienstag angerufen" in block
    assert "ruft am Freitag zurück" in block
    assert "Praxis meldet sich." in block


async def test_inbound_context_off_says_nothing(manager, tmp_path):
    """Default mode: a matched inbound call gets ZERO thread knowledge."""
    thread = await manager.create("Termin", language="de", primary_number="+4930222")
    await _seed_call(manager.db_path, "CA1", datetime.now(UTC) - timedelta(days=1), direction="outbound")
    await manager.attach("CA1", thread.thread_id, KIND_ORIGIN)
    await manager.update_after_call(
        "CA1",
        outcome=_outcome(),
        llm=FakeLLM({"summary": "Streng geheim.\nStand: offen.", "satisfied_commitments": []}),
        language="de",
    )
    assert await manager.build_context(thread.thread_id, "inbound", _settings(tmp_path)) == ""


async def test_inbound_context_ack_leaks_nothing_but_the_ack(manager, tmp_path):
    """§4.3: `ack` mode may acknowledge prior contact — and nothing else."""
    settings = _settings(tmp_path, thread_inbound_context="ack")
    thread = await manager.create("Zahnarzttermin Dr. Müller", language="de", primary_number="+4930222")
    await _seed_call(manager.db_path, "CA1", datetime(2026, 8, 18, 10, 0, tzinfo=UTC))
    await manager.attach("CA1", thread.thread_id, KIND_ORIGIN)
    await manager.update_after_call(
        "CA1",
        outcome=_outcome(
            task_result="Rechnung über 400 Euro offen.",
            commitments=[{"who": "callee", "what": "schickt die Rechnung", "when": None}],
        ),
        llm=FakeLLM({"summary": "Rechnung 400 Euro offen.\nStand: offen.", "satisfied_commitments": []}),
        language="de",
    )

    block = await manager.build_context(thread.thread_id, "inbound", settings)
    assert "Ich sehe, wir hatten dazu bereits Kontakt." in block
    for secret in ("Zahnarzttermin", "Dr. Müller", "Rechnung", "400", "2026-08-18", thread.thread_id):
        assert secret not in block, f"inbound ack must not disclose {secret!r}"
    assert len(block) <= th.MAX_CONTEXT_BLOCK


async def test_inbound_ack_requires_a_matched_thread(tmp_path):
    settings = _settings(tmp_path, thread_inbound_context="ack")
    assert ThreadManager._inbound_ack_block(settings, "") == ""


# ── Report decoration (§10) ──────────────────────────────────────────


async def test_decorate_report_header_open_and_resolution(manager, tmp_path):
    thread = await manager.create("Termin Dr. Müller", language="de")
    thread.open_commitments = [
        {"who": "callee", "what": "ruft zurück", "due": None, "status": "open", "source_call_sid": "CA1"}
    ]
    update = th.ThreadUpdate(thread=thread, call_index=2, call_total=3, commitments_changed=True, resolved_now=True)
    decorated = th.decorate_report("📞 Anruf beendet", update, _settings(tmp_path), "de")
    lines = decorated.splitlines()
    assert lines[0] == "🧵 Termin Dr. Müller — Anruf 2 von 3"
    assert "📞 Anruf beendet" in decorated
    assert "Offen: callee: ruft zurück" in decorated
    assert "✅ Anliegen erledigt." in decorated
    assert thread.thread_id in decorated


def test_decorate_report_is_identity_without_a_thread():
    assert th.decorate_report("plain report", None) == "plain report"


def test_thread_deep_link_uses_dashboard_when_configured():
    assert th.thread_deep_link(SimpleNamespace(dashboard_url="https://x.test/"), "thr_a") == (
        "https://x.test/voice/threads/thr_a"
    )
    assert th.thread_deep_link(SimpleNamespace(dashboard_url=""), "thr_a") == "thr_a"


# ── Helpers ──────────────────────────────────────────────────────────


def test_parse_commitments_drops_malformed_entries():
    parsed = th.parse_commitments(
        json.dumps(
            [
                {"who": "callee", "what": "ok", "status": "open"},
                {"who": "martian", "what": "nope"},
                {"who": "agent", "what": ""},
                "not a dict",
                {"who": "user", "what": "fine", "status": "bogus"},
            ]
        )
    )
    assert [(c["who"], c["what"], c["status"]) for c in parsed] == [
        ("callee", "ok", "open"),
        ("user", "fine", "open"),
    ]
    assert th.parse_commitments("{not json") == []


def test_truncate_marks_the_cut():
    assert th.truncate("short", 50) == "short"
    cut = th.truncate("word " * 100, 40)
    assert len(cut) <= 40 and cut.endswith("…")


# ── Chat tool (§8) ───────────────────────────────────────────────────


async def test_thread_lookup_tool(manager, tmp_path):
    th.set_thread_manager(manager)
    await manager.create("Termin Dr. Müller", contact_name="Dr. Müller", primary_number="+4930222")
    closed = await manager.create("Alte Sache", contact_name="Dr. Müller")
    await manager.close(closed.thread_id)

    lookup = th.make_thread_lookup(_settings(tmp_path))
    open_only = json.loads(await lookup("Müller"))
    assert [t["subject"] for t in open_only] == ["Termin Dr. Müller"]

    everything = json.loads(await lookup("Müller", status="all"))
    assert len(everything) == 2
    assert "Error" in await lookup("Müller", status="archived")


async def test_thread_lookup_is_tier_x_on_calls(manager):
    """§8: chat-only. On a call it must not even appear in the tool array."""
    from pincer.voice.tool_policy import TIER_X, TIERS

    assert TIERS["thread_lookup"] == TIER_X
