"""The voice write path on the repository/service layer, on both dialects."""

from __future__ import annotations

import uuid

import pytest
from support import seeded_id

from pincer.db.engine import get_engine
from pincer.db.session import session_scope
from pincer.models.voice import (
    CallAction,
    CallAnalytics,
    CallThread,
    CallThreadMember,
    CallTranscript,
    DoNotCallNumber,
    InboundMessage,
    OutboundCallLog,
    PhoneContact,
    VoiceCall,
)
from pincer.repositories.voice import ThreadMemberRepository
from pincer.services.voice import (
    AnalyticsService,
    CallsService,
    ContactsService,
    MessagesService,
    SafetyGateService,
    ThreadsService,
)

_TABLES = [
    model.__table__
    for model in (
        VoiceCall,
        CallTranscript,
        CallAction,
        CallAnalytics,
        PhoneContact,
        DoNotCallNumber,
        OutboundCallLog,
        InboundMessage,
        CallThread,
        CallThreadMember,
    )
]

EARLIER = "2026-09-01T10:00:00+00:00"
LATER = "2026-09-02T10:00:00+00:00"


@pytest.fixture
async def url(db_url: str):
    engine = get_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: VoiceCall.metadata.drop_all(c, tables=_TABLES))
        await conn.run_sync(lambda c: VoiceCall.metadata.create_all(c, tables=_TABLES))
    yield db_url
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: VoiceCall.metadata.drop_all(c, tables=_TABLES))


# ── calls ────────────────────────────────────────────────────────────


async def test_saving_a_call_twice_updates_it_in_place(url):
    calls = CallsService(url)
    await calls.save_call({"call_sid": "CA1", "direction": "inbound", "started_at": EARLIER, "language": "de"})
    await calls.save_call({"call_sid": "CA1", "direction": "inbound", "started_at": EARLIER, "ended_at": LATER})

    stored = await calls.get("CA1")
    assert stored["ended_at"] == LATER
    # A column this caller did not pass keeps its value — the INSERT OR REPLACE
    # this replaces would have blanked it.
    assert stored["language"] == "de"


async def test_the_thread_columns_are_re_derived_from_the_membership(url):
    """The member row is the durable record; the call's columns follow it."""
    calls = CallsService(url)
    async with session_scope(url) as session:
        session.add(
            CallThread(
                thread_id=seeded_id("th_1"), subject="s", origin="user_task", created_at=EARLIER, updated_at=EARLIER
            )
        )
        await session.flush()
        await ThreadMemberRepository(session).attach(
            {"call_sid": "CA1", "thread_id": seeded_id("th_1"), "attach_kind": "origin", "attached_at": EARLIER}
        )

    await calls.save_call(
        {"call_sid": "CA1", "direction": "outbound", "started_at": EARLIER}, thread_columns_from_members=True
    )

    stored = await calls.get("CA1")
    assert (stored["thread_id"], stored["thread_attach_kind"]) == (seeded_id("th_1"), "origin")
    # ... and the member row gains the call's own facts, which outlive it.
    async with session_scope(url) as session:
        member = await ThreadMemberRepository(session).for_call("CA1")
    assert (member.call_started_at, member.direction) == (EARLIER, "outbound")


async def test_a_call_with_no_thread_gets_empty_thread_columns(url):
    calls = CallsService(url)
    await calls.save_call(
        {"call_sid": "CA1", "direction": "inbound", "started_at": EARLIER}, thread_columns_from_members=True
    )
    stored = await calls.get("CA1")
    assert (stored["thread_id"], stored["thread_attach_kind"]) == (None, "")


async def test_transcript_lines_and_actions_round_trip(url):
    calls = CallsService(url)
    await calls.add_transcript_lines(
        [
            {"call_id": "CA1", "speaker": "caller", "text": "hello", "timestamp": EARLIER},
            {"call_id": "CA1", "speaker": "agent", "text": "hi", "timestamp": LATER},
            {"call_id": "CA2", "speaker": "agent", "text": "other call", "timestamp": EARLIER},
        ]
    )
    await calls.add_actions(
        [{"call_id": "CA1", "action_type": "tool_call", "tool_name": "calendar", "timestamp": EARLIER}]
    )

    assert [line["text"] for line in await calls.transcript_for("CA1")] == ["hello", "hi"]
    assert [action["tool_name"] for action in await calls.actions_for("CA1")] == ["calendar"]
    assert await calls.actions_for("CA2") == []
    assert await calls.add_transcript_lines([]) is None  # nothing to write is not an error


async def test_merging_a_thread_leaves_the_target_s_own_calls_alone(url):
    """Only the moved calls become `manual`: a call already in the target
    keeps the kind it was attached with."""
    calls = CallsService(url)
    threads = ThreadsService(url)
    for label in ("th_src", "th_dst"):
        await threads.create(
            {
                "thread_id": seeded_id(label),
                "subject": label,
                "origin": "user_task",
                "created_at": EARLIER,
                "updated_at": EARLIER,
            }
        )
    for sid, label, kind in (("CA_src", "th_src", "origin"), ("CA_dst", "th_dst", "inbound_matched")):
        await calls.save_call({"call_sid": sid, "direction": "inbound", "started_at": EARLIER})
        await threads.attach(
            {"call_sid": sid, "thread_id": seeded_id(label), "attach_kind": kind, "attached_at": EARLIER},
            touched_at=EARLIER,
        )

    await threads.merge_into(seeded_id("th_src"), seeded_id("th_dst"), attach_kind="manual", stamp=LATER)

    moved = await calls.get("CA_src")
    already_there = await calls.get("CA_dst")
    assert (moved["thread_id"], moved["thread_attach_kind"]) == (seeded_id("th_dst"), "manual")
    assert (already_there["thread_id"], already_there["thread_attach_kind"]) == (
        seeded_id("th_dst"),
        "inbound_matched",
    )
    # ... and the member rows say the same thing as the call rows.
    kinds = {row["call_sid"]: row["attach_kind"] for row in await threads.calls(seeded_id("th_dst"))}
    assert kinds == {"CA_src": "manual", "CA_dst": "inbound_matched"}


async def test_a_thread_id_that_could_never_exist_names_nothing(url):
    """`?thread_id=` comes straight from the dashboard: a bad one filters to no calls."""
    calls = CallsService(url)
    threads = ThreadsService(url)
    await calls.save_call({"call_sid": "CA_1", "direction": "inbound", "started_at": EARLIER})

    assert await calls.page_with_thread(thread_id="thr_0123abcd", limit=10, offset=0) == []
    assert await threads.calls("thr_0123abcd") == []
    assert await threads.set_fields("thr_0123abcd", {"subject": "x"}) == 0


async def test_a_message_survives_a_failing_intent_stamp(url, monkeypatch):
    """The label on the call is worth less than the message the caller left."""
    from pincer.repositories.voice import CallRepository

    async def _boom(*args, **kwargs):
        raise RuntimeError("no such column")

    monkeypatch.setattr(CallRepository, "set_fields", _boom)
    message_id = await MessagesService(url).record(
        {"call_sid": "CA1", "matter": "call back", "created_at": EARLIER}, inbound_intent="message"
    )

    assert uuid.UUID(message_id).version == 7
    async with session_scope(url) as session:
        from pincer.repositories.voice import InboundMessageRepository

        (row,) = await InboundMessageRepository(session).for_call("CA1")
    assert row.matter == "call back"


# ── the ordering the key is responsible for ──────────────────────────


async def test_utterances_spoken_in_one_second_keep_their_order(url):
    """`transcript_for` orders by `(timestamp, id)`, and a call's utterances
    routinely share a timestamp — so the key is what keeps them in the order
    they were spoken. A random key would scramble the transcript, which reads
    as the caller and the agent talking over each other.

    A UUIDv7 holds that line because it sorts by time and, inside one
    millisecond, by the generator's counter.
    """
    calls = CallsService(url)
    spoken = ["one", "two", "three", "four", "five"]
    await calls.add_transcript_lines(
        [{"call_id": "CA1", "speaker": "caller", "text": text, "timestamp": EARLIER} for text in spoken]
    )

    assert [line["text"] for line in await calls.transcript_for("CA1")] == spoken


async def test_messages_left_in_one_second_come_back_newest_first(url):
    """`newest` orders by `(created_at DESC, id DESC)`, same reasoning."""
    messages = MessagesService(url)
    for index in range(4):
        await messages.record({"call_sid": f"CA{index}", "matter": str(index), "created_at": EARLIER})

    assert [row["matter"] for row in await messages.newest(10)] == ["3", "2", "1", "0"]


# ── contacts ─────────────────────────────────────────────────────────


async def test_contacts_are_searched_case_insensitively(url):
    async with session_scope(url) as session:
        session.add_all(
            [
                PhoneContact(name="Dr. Müller", phone_number="+4930123", category="doctor", notes="private"),
                PhoneContact(name="mueller gmbh", phone_number="+4930999", category="supplier"),
                PhoneContact(name="Someone Else", phone_number="+4930000"),
            ]
        )

    found = await ContactsService(url).search("müll")
    assert found == [{"name": "Dr. Müller", "phone_number": "+4930123", "category": "doctor"}]
    # Notes are never returned: a lookup tool must not leak the rest of the row.
    assert "notes" not in found[0]
    assert await ContactsService(url).search("nobody") == []


# ── the dialling gate ────────────────────────────────────────────────


async def test_an_objection_is_recorded_once_and_keeps_its_first_date(url):
    gate = SafetyGateService(url)
    assert await gate.add_do_not_call(
        {"phone_number": "+4930111", "reason": "asked", "source": "call", "call_sid": "CA1", "added_at": EARLIER}
    )
    # A repeat is not new, and must not rewrite when the callee first objected.
    assert not await gate.add_do_not_call(
        {"phone_number": "+4930111", "reason": "again", "source": "manual", "call_sid": "CA2", "added_at": LATER}
    )

    (entry,) = await gate.list_do_not_call()
    assert (entry["added_at"], entry["reason"]) == (EARLIER, "again")
    assert await gate.is_do_not_call("+4930111")

    assert await gate.remove_do_not_call("+4930111")
    assert not await gate.is_do_not_call("+4930111")
    assert not await gate.remove_do_not_call("+4930111")


async def test_the_dial_log_feeds_the_cap_and_the_cooldown(url):
    gate = SafetyGateService(url)
    for placed_at, day, number in (
        (EARLIER, "2026-09-01", "+4930111"),
        (LATER, "2026-09-02", "+4930111"),
        (LATER, "2026-09-02", "+4930222"),
    ):
        await gate.log_outbound(
            {
                "phone_number": number,
                "user_id": "u",
                "channel": "cli",
                "call_sid": "",
                "placed_at": placed_at,
                "local_day": day,
            }
        )

    assert await gate.calls_today("2026-09-02") == 2
    assert await gate.calls_today("2026-09-03") == 0
    assert await gate.placed_since("+4930111", EARLIER) == [EARLIER, LATER]
    assert await gate.placed_since("+4930111", LATER) == [LATER]


# ── receptionist messages ────────────────────────────────────────────


async def test_a_retaken_message_replaces_the_earlier_one_and_stamps_the_call(url):
    await CallsService(url).save_call({"call_sid": "CA1", "direction": "inbound", "started_at": EARLIER})
    messages = MessagesService(url)

    await messages.record({"call_sid": "CA1", "matter": "first", "created_at": EARLIER}, inbound_intent="message")
    message_id = await messages.record({"call_sid": "CA1", "matter": "second", "created_at": LATER})

    assert uuid.UUID(message_id).version == 7
    async with session_scope(url) as session:
        from pincer.repositories.voice import InboundMessageRepository

        rows = await InboundMessageRepository(session).for_call("CA1")
    assert [row.matter for row in rows] == ["second"]
    assert (await CallsService(url).get("CA1"))["inbound_intent"] == "message"


async def test_marking_a_message_delivered(url):
    messages = MessagesService(url)
    await messages.record({"call_sid": "CA1", "matter": "call back", "created_at": EARLIER})
    await messages.mark_delivered("CA1", LATER)

    async with session_scope(url) as session:
        from pincer.repositories.voice import InboundMessageRepository

        (row,) = await InboundMessageRepository(session).for_call("CA1")
    assert row.delivered_to_owner_at == LATER


# ── analytics ────────────────────────────────────────────────────────


async def test_analytics_are_replaced_on_re_analysis_and_read_in_batches(url):
    calls = CallsService(url)
    analytics = AnalyticsService(url)
    for sid, started in (("CA1", EARLIER), ("CA2", LATER)):
        await calls.save_call({"call_sid": sid, "direction": "inbound", "started_at": started})

    await analytics.save({"call_sid": "CA1", "method": "estimated", "sentiment": "neutral", "created_at": EARLIER})
    await analytics.save({"call_sid": "CA1", "method": "exact", "sentiment": "negative", "created_at": LATER})
    await analytics.save({"call_sid": "CA2", "method": "exact", "sentiment": "positive", "created_at": LATER})

    assert (await analytics.get("CA1"))["method"] == "exact"
    assert set(await analytics.for_calls(["CA1", "CA2", "CA-missing"])) == {"CA1", "CA2"}
    assert await analytics.for_calls([]) == {}


async def test_sentiment_counts_join_the_call_for_its_window_and_direction(url):
    calls = CallsService(url)
    analytics = AnalyticsService(url)
    await calls.save_call({"call_sid": "CA_old", "direction": "inbound", "started_at": EARLIER})
    await calls.save_call({"call_sid": "CA_new", "direction": "inbound", "started_at": LATER})
    await calls.save_call({"call_sid": "CA_out", "direction": "outbound", "started_at": LATER})
    for sid, sentiment in (("CA_old", "negative"), ("CA_new", "negative"), ("CA_out", "positive")):
        await analytics.save({"call_sid": sid, "method": "exact", "sentiment": sentiment, "created_at": LATER})

    assert await analytics.sentiment_counts(EARLIER) == {"negative": 2, "positive": 1}
    assert await analytics.sentiment_counts(LATER) == {"negative": 1, "positive": 1}
    assert await analytics.sentiment_counts(EARLIER, direction="outbound") == {"positive": 1}
    assert await analytics.negative_since(LATER) == 1
