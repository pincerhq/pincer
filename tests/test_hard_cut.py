"""No message cap, and the token emergency cut (issue #358, slice 5, decision D7)."""

from __future__ import annotations

import json
import logging

import pytest
from anthropic_sse import ScriptedAnthropic, text

from pincer.core.agent import Agent
from pincer.core.compaction import CompactionOutcome, Compactor, LastRequest, SentRequest, ServerCompaction
from pincer.core.session import SessionManager
from pincer.llm.base import ImageContent, LLMMessage, LLMResponse, MessageRole
from pincer.tools.registry import ToolRegistry


async def test_a_session_of_200_messages_is_never_trimmed_and_every_request_carries_it(
    settings, cost_tracker, tmp_path
):
    sessions = SessionManager(tmp_path / "long.db")
    await sessions.initialize()
    try:
        session = await sessions.get_or_create("user1", "test")
        for index in range(200):
            role = MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT
            await sessions.add_message(session, LLMMessage(role=role, content=f"message {index}"))
        assert len(session.messages) == 200  # precondition: outside a turn, past the old cap of 50
        async with ScriptedAnthropic(settings, [([text("Reply")], "end_turn")]) as llm:
            agent = Agent(settings, llm.provider, sessions, cost_tracker, ToolRegistry())
            await agent.handle_message("user1", "test", "turn 1", memory=False)

        assert len(session.messages) == 202
        assert [m["content"] for m in llm.requests[0]["messages"]][:2] == ["message 0", "message 1"]
        assert len(llm.requests[0]["messages"]) == 201  # all 200 earlier messages and the new one
        row = await sessions._store.load("test:user1", user_id="user1", channel="test", legacy_fallback=False)
        assert row is not None
        assert len(json.loads(row["messages"])) == 202
    finally:
        await sessions.close()


class _Router:
    """The router as `build_compactor` uses it, with a summary model."""

    def __init__(self, provider) -> None:
        self._provider = provider

    def get_summarizer(self):
        return self._provider, "claude-haiku-4-5-20251001"

    def get_provider(self, name: str, model_hint: str = ""):
        return self._provider

    def is_free(self, name: str) -> bool:
        return False


_DEPRECATED = (
    "PINCER_MAX_SESSION_MESSAGES no longer has an effect: sessions are no longer cut by message count, "
    "they are compacted by prompt tokens (PINCER_COMPACT_AT_TOKENS, PINCER_COMPACT_HARD_TOKENS)"
)


@pytest.mark.parametrize(("value", "warned"), [(20, True), (None, False)])
async def test_a_max_session_messages_still_set_is_warned_about_once_at_startup(
    settings, session_manager, cost_tracker, caplog, value, warned
):
    from pincer.core.compaction import build_compactor

    settings.max_session_messages = value
    async with ScriptedAnthropic(settings, []) as llm:
        build_compactor(settings, _Router(llm.provider), cost_tracker, session_manager, None)

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == ([_DEPRECATED] if warned else [])


@pytest.mark.parametrize(
    ("limits", "error"),
    [
        (
            {"compact_hard_tokens": 80_000},
            "Value error, compact_hard_tokens (80000) must be above compact_at_tokens (80000)",
        ),
        (
            {"compact_hard_tokens": 60_000},
            "Value error, compact_hard_tokens (60000) must be above compact_at_tokens (80000)",
        ),
        (
            {"compact_to_tokens": 80_000},
            "Value error, compact_to_tokens (80000) must be below compact_at_tokens (80000)",
        ),
        (
            {"compact_at_tokens": 200_000},
            "Value error, compact_hard_tokens (160000) must be above compact_at_tokens (200000)",
        ),
    ],
    ids=["hard equal to at", "hard below at", "to equal to at", "at raised past the default hard"],
)
def test_the_limits_must_run_hard_above_at_above_to(limits, error):
    from pydantic import ValidationError

    from pincer.config.tools import ToolSettings

    with pytest.raises(ValidationError) as refused:
        ToolSettings(**limits)

    assert [e["msg"] for e in refused.value.errors()] == [error]


def test_the_limits_default_to_160k_80k_50k_and_the_hard_one_is_read_from_the_environment(monkeypatch):
    from pincer.config.tools import ToolSettings

    defaults = ToolSettings()
    assert (defaults.compact_hard_tokens, defaults.compact_at_tokens, defaults.compact_to_tokens) == (
        160_000,
        80_000,
        50_000,
    )
    monkeypatch.setenv("PINCER_COMPACT_HARD_TOKENS", "200000")
    from pincer.config import Settings

    assert Settings(_env_file=None).compact_hard_tokens == 200_000  # type: ignore[call-arg]


# ── what compact() reports, exit by exit ─────────────────────────────────────

_BIG = 40_000  # characters per earlier message
_SUMMARY = "SUMMARY-OF-THE-EARLIER-TALK"
_HAIKU = "claude-haiku-5-5"


def _earlier(index: int) -> str:
    head = f"message {index} "
    return head + "x" * (_BIG - len(head))


async def _history(sessions: SessionManager, count: int = 10, photos: dict[int, bytes] | None = None):
    """A session of `count` big earlier messages (user first, alternating), then "turn 1" and "Reply 1".
    Returns it with the messages the turn's last request sent: all but the reply."""
    session = await sessions.get_or_create("user1", "test")
    for index in range(count):
        role = MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT
        images = [ImageContent.from_bytes(photos[index], "image/jpeg")] if photos and index in photos else []
        await sessions.add_message(session, LLMMessage(role=role, content=_earlier(index), images=images))
    await sessions.add_message(session, LLMMessage(role=MessageRole.USER, content="turn 1"))
    sent = list(session.messages)
    await sessions.add_message(session, LLMMessage(role=MessageRole.ASSISTANT, content="Reply 1"))
    return session, sent


def _last(prompt_tokens: int, sent: list[LLMMessage], *, server: bool) -> LastRequest:
    request = SentRequest("anthropic", _HAIKU, None, None) if server else None
    return LastRequest.measure(prompt_tokens, sent, 2_000, request=request)


def _compactor(settings, costs, sessions, *, llm=None, leaf=None, hard: int | None = None) -> Compactor:
    server = ServerCompaction(settings, lambda _name, _model: leaf, lambda _name: False) if leaf else None
    return Compactor(
        llm,
        None,
        costs,
        sessions,
        None,
        provider="anthropic",
        is_free=False,
        at_tokens=80_000,
        to_tokens=50_000,
        hard_tokens=hard,
        server=server,
    )


class _Summarizer:
    """The summary model of the client strategy: answers, fails, or edits the session while it answers."""

    def __init__(self, *, fail: bool = False, edit=None) -> None:
        self._fail = fail
        self._edit = edit

    async def complete(self, **_kwargs):
        from pincer.exceptions import LLMError

        if self._fail:
            raise LLMError("summary model is down")
        if self._edit is not None:
            self._edit()
        return LLMResponse(content=_SUMMARY, provider="anthropic", request_model="claude-haiku-4-5-20251001")


async def _spent(tmp_path):
    """A cost tracker whose daily budget is already spent."""
    from pincer.config.pricing import load_price_table
    from pincer.llm.cost_tracker import CostTracker

    costs = CostTracker(tmp_path / "spent.db", daily_budget=1e-9, prices=load_price_table())
    await costs.initialize()
    await costs.record("anthropic", "claude-haiku-4-5-20251001", LLMResponse(input_tokens=1_000).usage)
    assert await costs.budget_reached()  # precondition
    return costs


_COMPACTED_REPLY = ([{"type": "compaction", "content": _SUMMARY, "signature": "SIG"}], "compaction")
_EXITS = [
    # (id, outcome, client or server, scenario)
    ("not due", CompactionOutcome.SKIPPED, "client", {"prompt": 70_000}),
    ("client: summary replaced the oldest", CompactionOutcome.COMPACTED, "client", {}),
    ("client: no summary model", CompactionOutcome.FAILED, "client", {"llm": None}),
    ("client: budget reached", CompactionOutcome.FAILED, "client", {"budget": True}),
    ("client: summary call failed", CompactionOutcome.FAILED, "client", {"llm": _Summarizer(fail=True)}),
    ("client: nothing to cut", CompactionOutcome.SKIPPED, "client", {"count": 0}),
    ("client: swap discarded", CompactionOutcome.SKIPPED, "client", {"edit": True}),
    ("server: summary replaced the oldest", CompactionOutcome.COMPACTED, "server", {"script": [_COMPACTED_REPLY]}),
    ("server: budget reached", CompactionOutcome.FAILED, "server", {"budget": True, "script": []}),
    ("server: a user message follows", CompactionOutcome.FAILED, "server", {"follow": True, "script": []}),
    ("server: call raised", CompactionOutcome.FAILED, "server", {"script": [400]}),
    ("server: no summary", CompactionOutcome.FAILED, "server", {"script": [([], "max_tokens")]}),
    ("server: nothing was sent", CompactionOutcome.SKIPPED, "server", {"nothing_sent": True, "script": []}),
    ("server: swap discarded", CompactionOutcome.SKIPPED, "server", {"edit": True, "script": [_COMPACTED_REPLY]}),
]


@pytest.mark.parametrize(("outcome", "kind", "case"), [e[1:] for e in _EXITS], ids=[e[0] for e in _EXITS])
async def test_compact_reports_the_outcome_of_every_exit(
    settings, session_manager, cost_tracker, tmp_path, outcome, kind, case
):
    import asyncio

    session, sent = await _history(session_manager, count=case.get("count", 10))
    costs = await _spent(tmp_path) if case.get("budget") else cost_tracker
    if case.get("follow"):
        await session_manager.add_message(session, LLMMessage(role=MessageRole.USER, content="four"))
    last = _last(case.get("prompt", 100_000), [] if case.get("nothing_sent") else sent, server=kind == "server")

    def edit_head() -> None:  # as a tool-pair repair would: the oldest message is a new object
        session.messages = [LLMMessage(role=MessageRole.USER, content="edited"), *session.messages[1:]]

    try:
        if kind == "client":
            llm = case["llm"] if "llm" in case else _Summarizer(edit=edit_head if case.get("edit") else None)
            result = await _compactor(settings, costs, session_manager, llm=llm).compact(session, last)
        else:
            async with ScriptedAnthropic(settings, case["script"]) as scripted:
                compactor = _compactor(settings, costs, session_manager, leaf=scripted.provider)
                if case.get("edit"):
                    gate = scripted.gates[1] = asyncio.Event()
                    task = asyncio.create_task(compactor.compact(session, last))
                    async with asyncio.timeout(5):
                        while not scripted.requests:
                            await asyncio.sleep(0.01)
                    edit_head()
                    gate.set()
                    result = await asyncio.wait_for(task, timeout=5)
                else:
                    result = await compactor.compact(session, last)
    finally:
        if costs is not cost_tracker:
            await costs.close()

    assert result is outcome


# ── the emergency cut ────────────────────────────────────────────────────────

_CUT_WARNING = (
    "Session test:user1: prompt of 170000 tokens is over the hard limit of 160000 and compaction failed; "
    "dropped the oldest 8 messages"
)


def _estimate(last: LastRequest, messages: list[LLMMessage]) -> float:
    from pincer.core.compaction import _chars

    return last.tokens_per_char * (last.overhead_chars + sum(_chars(m) for m in messages))


async def _photo_shas(sessions: SessionManager) -> list[str]:
    row = await sessions._store.load("test:user1", user_id="user1", channel="test", legacy_fallback=False)
    assert row is not None
    return sorted(photo.sha256 for photo in row["images"])


async def test_a_failed_compaction_over_the_hard_limit_cuts_the_oldest_turns_and_their_photos(
    settings, session_manager, cost_tracker, caplog
):
    caplog.set_level(logging.WARNING, logger="pincer.core.compaction")
    old, new = b"photo-in-a-cut-message", b"photo-in-a-kept-message"
    session, sent = await _history(session_manager, photos={0: old, 9: new})
    last = _last(170_000, sent, server=True)
    before = _estimate(last, session.messages)
    async with ScriptedAnthropic(settings, [400]) as scripted:  # the server refuses to compact
        compactor = _compactor(settings, cost_tracker, session_manager, leaf=scripted.provider, hard=160_000)
        outcome = await compactor.compact(session, last)

    assert scripted.requests  # precondition: the server was asked first
    assert before > 160_000  # precondition: the prompt is over the hard limit
    assert outcome is CompactionOutcome.CUT
    assert [m.content[:9] for m in session.messages] == ["message 8", "message 9", "turn 1", "Reply 1"]
    assert session.messages[0].role is MessageRole.USER  # whole turns: the kept history starts with a user message
    assert _estimate(last, session.messages) <= 50_000
    assert [r.getMessage() for r in caplog.records if "hard limit" in r.getMessage()] == [_CUT_WARNING]
    import hashlib

    assert await _photo_shas(session_manager) == [hashlib.sha256(new).hexdigest()]  # the cut photo is gone
    print(f"\nHARD CUT: estimate {before:.0f} -> {_estimate(last, session.messages):.0f}; kept {len(session.messages)}")


@pytest.mark.parametrize("failure", ["budget reached", "no summary model", "summary call failed"])
async def test_every_other_way_compaction_fails_over_the_hard_limit_cuts_too(
    settings, session_manager, cost_tracker, tmp_path, failure
):
    session, sent = await _history(session_manager)
    last = _last(170_000, sent, server=False)
    costs = await _spent(tmp_path) if failure == "budget reached" else cost_tracker
    llm = {"budget reached": _Summarizer(), "no summary model": None, "summary call failed": _Summarizer(fail=True)}
    try:
        outcome = await _compactor(settings, costs, session_manager, llm=llm[failure], hard=160_000).compact(
            session, last
        )
    finally:
        if costs is not cost_tracker:
            await costs.close()

    assert outcome is CompactionOutcome.CUT
    assert [m.content[:9] for m in session.messages] == ["message 8", "message 9", "turn 1", "Reply 1"]


async def test_a_compaction_that_worked_or_a_prompt_within_the_hard_limit_cuts_nothing(
    settings, session_manager, cost_tracker
):
    session, sent = await _history(session_manager)
    whole = list(session.messages)
    over = _last(170_000, sent, server=False)
    # compaction succeeded above the hard limit: the summary is the change, no cut follows
    assert (
        await _compactor(settings, cost_tracker, session_manager, llm=_Summarizer(), hard=160_000).compact(
            session, over
        )
        is CompactionOutcome.COMPACTED
    )
    assert session.messages[0].role is MessageRole.SYSTEM  # summary + the kept messages, not a cut
    session.messages = list(whole)
    # a failed compaction at or below the hard limit changes nothing and is retried after a later turn
    for tokens in (100_000, 160_000):
        outcome = await _compactor(
            settings, cost_tracker, session_manager, llm=_Summarizer(fail=True), hard=160_000
        ).compact(session, _last(tokens, sent, server=False))
        assert outcome is CompactionOutcome.FAILED
        assert session.messages == whole


@pytest.mark.parametrize("pairs", [0, 1, 2, 3])
async def test_the_cut_keeps_tool_pairs_whole_and_lands_on_a_user_message(
    settings, session_manager, cost_tracker, pairs
):
    """`pairs` tool calls sit at the start of the kept side's neighbourhood: the cut must not split them."""
    from pincer.llm.base import ToolCall

    session = await session_manager.get_or_create("user1", "test")
    for turn in range(6):
        await session_manager.add_message(session, LLMMessage(role=MessageRole.USER, content=_earlier(turn)))
        for n in range(pairs if turn % 2 else 0):
            call = ToolCall(id=f"toolu_{turn}_{n}", name="create_event", arguments={})
            await session_manager.add_message(
                session, LLMMessage(role=MessageRole.ASSISTANT, content="calling", tool_calls=[call])
            )
            await session_manager.add_message(
                session,
                LLMMessage(role=MessageRole.TOOL_RESULT, content="done", tool_call_id=call.id),
            )
        await session_manager.add_message(session, LLMMessage(role=MessageRole.ASSISTANT, content="reply"))
    last = _last(170_000, list(session.messages), server=False)
    outcome = await _compactor(
        settings, cost_tracker, session_manager, llm=_Summarizer(fail=True), hard=160_000
    ).compact(session, last)

    assert outcome is CompactionOutcome.CUT
    kept = session.messages
    assert kept[0].role is MessageRole.USER
    results = {m.tool_call_id for m in kept if m.role is MessageRole.TOOL_RESULT}
    calls = {tc.id for m in kept for tc in m.tool_calls}
    assert results == calls  # no orphan on either side
    assert _estimate(last, kept) <= 50_000


async def test_one_giant_turn_cannot_be_cut(settings, session_manager, cost_tracker, caplog):
    caplog.set_level(logging.WARNING, logger="pincer.core.compaction")
    session, sent = await _history(session_manager, count=0)  # only "turn 1" and its reply
    whole = list(session.messages)
    last = _last(170_000, sent, server=True)
    async with ScriptedAnthropic(settings, [400]) as scripted:
        compactor = _compactor(settings, cost_tracker, session_manager, leaf=scripted.provider, hard=160_000)
        outcome = await compactor.compact(session, last)

    assert outcome is CompactionOutcome.FAILED
    assert session.messages == whole
    assert any("no earlier turn to drop" in r.getMessage() for r in caplog.records)


async def test_a_cut_is_discarded_when_the_history_changed_meanwhile(settings, session_manager, cost_tracker):
    session, _sent = await _history(session_manager)
    prefix = list(session.messages[:8])
    session.messages = [LLMMessage(role=MessageRole.USER, content="edited"), *session.messages[1:]]

    assert not await session_manager.cut(session, prefix)
    assert len(session.messages) == 12  # untouched


async def test_the_built_compactor_enables_the_hard_cut_from_the_settings(settings, session_manager, cost_tracker):
    from pincer.core.compaction import build_compactor

    settings.compact_hard_tokens = 120_000
    session, sent = await _history(session_manager)
    async with ScriptedAnthropic(settings, [400]) as scripted:
        compactor = build_compactor(settings, _Router(scripted.provider), cost_tracker, session_manager, None)
        outcome = await compactor.compact(session, _last(130_000, sent, server=True))  # over 120K, under 160K

    assert outcome is CompactionOutcome.CUT


# ── the leading summary survives the cut (PR #379 review) ────────────────────

_SUMMARY_KINDS = {
    "client summary": {},
    "anthropic compaction block": {
        "raw": [{"type": "compaction", "content": "SUM", "signature": "SIG"}],
        "wire": "anthropic:claude-haiku-5-5",
    },
    "openai window": {"raw": [{"type": "compaction", "encrypted_content": "ENC"}], "wire": "openai:gpt-6"},
}


def _summary(kind: str) -> LLMMessage:
    return LLMMessage(role=MessageRole.SYSTEM, content="[Previous conversation summary]\nSUM", **_SUMMARY_KINDS[kind])


def test_the_hard_cut_starts_after_the_leading_summary():
    from pincer.core.compaction import choose_hard_cut

    messages = [_summary("client summary")]
    for _turn in range(6):
        messages.append(LLMMessage(role=MessageRole.USER, content="u" * 2_000))
        messages.append(LLMMessage(role=MessageRole.ASSISTANT, content="a" * 2_000))
    last = LastRequest.measure(1_000_000, messages, overhead_chars=0)
    whole = last.tokens_per_char * sum(len(m.content) for m in messages)

    cut = choose_hard_cut(messages, last, target_tokens=int(whole) - 1)  # one message too many

    assert cut == 3  # the summary stays: the first turn goes (its user message, its reply)
    assert messages[cut].role is MessageRole.USER


def test_nothing_but_the_summary_to_drop_means_no_cut():
    from pincer.core.compaction import choose_hard_cut

    messages = [
        _summary("client summary"),
        LLMMessage(role=MessageRole.USER, content="u" * 90_000),
        LLMMessage(role=MessageRole.ASSISTANT, content="a" * 90_000),
    ]
    last = LastRequest.measure(170_000, messages, overhead_chars=0)

    assert choose_hard_cut(messages, last, target_tokens=50_000) == 0


@pytest.mark.parametrize("kind", list(_SUMMARY_KINDS))
async def test_the_emergency_cut_keeps_the_summary_of_the_conversation_before_it(
    settings, session_manager, cost_tracker, caplog, kind
):
    caplog.set_level(logging.WARNING, logger="pincer.core.compaction")
    session = await session_manager.get_or_create("user1", "test")
    summary = _summary(kind)
    await session_manager.add_message(session, summary)
    for index in range(10):
        role = MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT
        await session_manager.add_message(session, LLMMessage(role=role, content=_earlier(index)))
    await session_manager.add_message(session, LLMMessage(role=MessageRole.USER, content="turn 1"))
    sent = list(session.messages)
    await session_manager.add_message(session, LLMMessage(role=MessageRole.ASSISTANT, content="Reply 1"))
    last = _last(170_000, sent, server=False)

    outcome = await _compactor(
        settings, cost_tracker, session_manager, llm=_Summarizer(fail=True), hard=160_000
    ).compact(session, last)

    assert outcome is CompactionOutcome.CUT
    assert session.messages[0] is summary  # the condensed memory of the earlier conversation survives
    assert [r.getMessage() for r in caplog.records if "hard limit" in r.getMessage()] == [_CUT_WARNING]  # 8 dropped
    assert [m.content[:9] for m in session.messages[1:]] == ["message 8", "message 9", "turn 1", "Reply 1"]
    row = await session_manager._store.load("test:user1", user_id="user1", channel="test", legacy_fallback=False)
    assert row is not None
    stored = json.loads(row["messages"])
    assert [m["role"] for m in stored][:2] == ["system", "user"]
    assert stored[0]["content"] == summary.content
    assert stored[0].get("raw", []) == summary.raw
