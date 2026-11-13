"""A conversation on an Anthropic-wire endpoint keeps the model's thinking across turns (issue #358, slice 1)."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from anthropic_sse import ScriptedAnthropic, redacted, text, thinking, tool_use

from pincer.core.agent import Agent
from pincer.core.onboarding import ONBOARDING_QUESTION_EN
from pincer.core.session import SessionManager
from pincer.llm.base import LLMMessage, LLMResponse, MessageRole
from pincer.tools.registry import ToolRegistry

_THOUGHT = "SECRET-THOUGHT-7f3a"
_SIGNATURE = "SIG-7f3a-signature"
_THOUGHT_BLOCK = {"type": "thinking", "thinking": _THOUGHT, "signature": _SIGNATURE}


@pytest.fixture
def registry() -> ToolRegistry:
    registry = ToolRegistry()

    async def create_event(title: str = "") -> str:
        return "created"

    registry.register(name="create_event", description="Create a calendar event", handler=create_event)
    return registry


def _assistant_contents(request: dict[str, Any]) -> list[Any]:
    return [m["content"] for m in request["messages"] if m["role"] == "assistant"]


async def test_chat_turn_two_carries_turn_ones_thinking_verbatim(settings, session_manager, cost_tracker, registry):
    script = [
        ([thinking(_THOUGHT, _SIGNATURE), text("Hello there.")], "end_turn"),
        ([text("You're welcome.")], "end_turn"),
    ]
    async with ScriptedAnthropic(settings, script) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry)
        await agent.handle_message("user1", "test", "Hi", memory=False)
        await agent.handle_message("user1", "test", "Thanks", memory=False)

    assert len(llm.requests) == 2
    assert _assistant_contents(llm.requests[0]) == []  # precondition: turn 1 sent no earlier reply
    assert _assistant_contents(llm.requests[1]) == [[_THOUGHT_BLOCK, {"type": "text", "text": "Hello there."}]]


_REDACTED = "REDACTED-7f3a-data"
_TOOL_TURN = [
    (
        [
            text("Checking."),
            thinking(_THOUGHT, _SIGNATURE),
            redacted(_REDACTED),
            tool_use("toolu_1", "create_event", {"title": "Dentist"}),
        ],
        "tool_use",
    ),
    ([thinking("SECOND-THOUGHT", "SIG-2"), text("Booked.")], "end_turn"),
    ([text("You're welcome.")], "end_turn"),
]
_TOOL_TURN_BLOCKS = [
    {"type": "text", "text": "Checking."},
    _THOUGHT_BLOCK,
    {"type": "redacted_thinking", "data": _REDACTED},
    {"type": "tool_use", "id": "toolu_1", "name": "create_event", "input": {"title": "Dentist"}},
]
_TOOL_TURN_REPLY_BLOCKS = [{"type": "thinking", "thinking": "SECOND-THOUGHT", "signature": "SIG-2"}, text("Booked.")]


async def test_chat_tool_turn_replays_every_block_in_its_own_order_on_the_next_turn(
    settings, session_manager, cost_tracker, registry
):
    async with ScriptedAnthropic(settings, _TOOL_TURN) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry)
        await agent.handle_message("user1", "test", "Book the dentist", memory=False)
        await agent.handle_message("user1", "test", "Thanks", memory=False)

    assert len(llm.requests) == 3
    assert _assistant_contents(llm.requests[1]) == [_TOOL_TURN_BLOCKS]  # precondition: within the turn
    assert _assistant_contents(llm.requests[2]) == [_TOOL_TURN_BLOCKS, _TOOL_TURN_REPLY_BLOCKS]


async def test_streaming_chat_tool_turn_replays_every_block_on_the_next_turn(
    settings, session_manager, cost_tracker, registry
):
    async with ScriptedAnthropic(settings, _TOOL_TURN) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry)
        _first = [c async for c in agent.handle_message_stream("user1", "web", "Book the dentist", memory=False)]
        _second = [c async for c in agent.handle_message_stream("user1", "web", "Thanks", memory=False)]

    assert len(llm.requests) == 3
    assert _assistant_contents(llm.requests[1]) == [_TOOL_TURN_BLOCKS]  # precondition: within the turn
    assert _assistant_contents(llm.requests[2]) == [_TOOL_TURN_BLOCKS, _TOOL_TURN_REPLY_BLOCKS]


async def test_voice_turn_replays_every_block_on_the_next_turn_without_repeating_the_spoken_text(
    settings, session_manager, cost_tracker, registry
):
    settings.voice_turn_model = ""  # hermetic: a developer .env may set an override
    settings.voice_max_response_tokens = 220
    script = [
        (
            [text("Einen Moment."), thinking(_THOUGHT, _SIGNATURE), tool_use("toolu_1", "create_event", {})],
            "tool_use",
        ),
        ([thinking("SECOND-THOUGHT", "SIG-2"), text("Erledigt.")], "end_turn"),
        ([text("Gern.")], "end_turn"),
    ]
    async with ScriptedAnthropic(settings, script) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry)
        first = [c async for c in agent.stream_voice_turn("+4930111222", "voice", "Termin beim Zahnarzt")]
        _second = [c async for c in agent.stream_voice_turn("+4930111222", "voice", "Danke")]

    assert first[-1].content == "Einen Moment.Erledigt."  # precondition: the spoken text spans both steps
    assert len(llm.requests) == 3
    assert _assistant_contents(llm.requests[2]) == [
        [
            {"type": "text", "text": "Einen Moment."},
            _THOUGHT_BLOCK,
            {"type": "tool_use", "id": "toolu_1", "name": "create_event", "input": {}},
        ],
        _TOOL_TURN_REPLY_BLOCKS[:1] + [{"type": "text", "text": "Erledigt."}],
    ]


async def test_turn_three_after_a_restart_still_sends_turns_one_and_twos_thinking(
    settings, session_manager, cost_tracker, registry, tmp_path
):
    turn_two_block = {"type": "thinking", "thinking": "TURN-TWO-THOUGHT", "signature": "SIG-T2"}
    script = [*_TOOL_TURN[:2], ([turn_two_block, text("Gern.")], "end_turn"), ([text("Bitte.")], "end_turn")]
    async with ScriptedAnthropic(settings, script) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry)
        await agent.handle_message("user1", "test", "Book the dentist", memory=False)
        await agent.handle_message("user1", "test", "Thanks", memory=False)
        await session_manager.close()  # the process ends: the cached session is gone

        restarted = SessionManager(tmp_path / "test.db")
        await restarted.initialize()
        try:
            await Agent(settings, llm.provider, restarted, cost_tracker, registry).handle_message(
                "user1", "test", "Danke", memory=False
            )
        finally:
            await restarted.close()

    assert len(llm.requests) == 4
    assert _assistant_contents(llm.requests[3]) == [
        _TOOL_TURN_BLOCKS,
        _TOOL_TURN_REPLY_BLOCKS,
        [turn_two_block, {"type": "text", "text": "Gern."}],
    ]


async def test_a_session_saved_before_raw_existed_loads_and_replays_as_before(
    settings, session_manager, cost_tracker, registry
):
    legacy = [
        {"role": "user", "content": "Book the dentist"},
        {
            "role": "assistant",
            "content": "Checking.",
            "tool_calls": [{"id": "toolu_1", "name": "create_event", "arguments": {"title": "Dentist"}}],
        },
        {"role": "tool_result", "content": "created", "tool_call_id": "toolu_1"},
        {"role": "assistant", "content": "Booked."},
    ]
    await session_manager._store.save(
        session_id="test:user1",
        user_id="user1",
        channel="test",
        messages=json.dumps(legacy),
        metadata="{}",
        created_at=1.0,
        updated_at=1.0,
    )
    async with ScriptedAnthropic(settings, [([text("You're welcome.")], "end_turn")]) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry)
        await agent.handle_message("user1", "test", "Thanks", memory=False)

    assert _assistant_contents(llm.requests[0]) == [
        [
            {"type": "text", "text": "Checking."},
            {"type": "tool_use", "id": "toolu_1", "name": "create_event", "input": {"title": "Dentist"}},
        ],
        "Booked.",
    ]


@pytest.mark.parametrize(
    ("second_provider", "second_model"),
    [("anthropic", "claude-b"), ("foundry", "claude-a")],
    ids=["same provider, other model", "other provider, same model"],
)
async def test_a_message_from_another_wire_is_rebuilt_and_sends_no_thinking(
    settings, session_manager, cost_tracker, registry, second_provider, second_model
):
    first_settings = settings.model_copy(update={"anthropic_model": "claude-a"})
    second_settings = settings.model_copy(
        update={
            "anthropic_model": second_model,
            "anthropic_compatible_provider": "foundry",
            "anthropic_compatible_base_url": "http://foundry.test",
            "anthropic_compatible_model": second_model,
        }
    )
    async with (
        ScriptedAnthropic(first_settings, _TOOL_TURN[:2]) as first,
        ScriptedAnthropic(second_settings, [([text("Gern.")], "end_turn")], provider=second_provider) as second,
    ):
        await Agent(settings, first.provider, session_manager, cost_tracker, registry).handle_message(
            "user1", "test", "Book the dentist", memory=False
        )
        await Agent(settings, second.provider, session_manager, cost_tracker, registry).handle_message(
            "user1", "test", "Thanks", memory=False
        )

    assert _assistant_contents(first.requests[1]) == [
        _TOOL_TURN_BLOCKS
    ]  # precondition: the thinking was sent on its own wire
    assert _assistant_contents(second.requests[0]) == [
        [
            {"type": "text", "text": "Checking."},
            {"type": "tool_use", "id": "toolu_1", "name": "create_event", "input": {"title": "Dentist"}},
        ],
        "Booked.",
    ]


_BETA = "thinking-binding-controls-2026-08-01"
_DROP_BLOCK = {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
_HAIKU, _SONNET, _OPUS_4_6, _PROXY_MODEL = "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-4-6", "proxy-model"


async def _sent(settings, *, model: str, thinking: str | None = None, provider: str = "anthropic"):
    """One request's (body, headers, url) from a provider whose endpoint is scripted."""
    async with ScriptedAnthropic(settings, [([text("ok")], "end_turn")], provider=provider) as llm:
        await llm.provider.complete([LLMMessage(role=MessageRole.USER, content="hi")], model=model, thinking=thinking)
    return llm.requests[0], llm.headers[0], llm.urls[0]


@pytest.mark.parametrize(
    ("model", "thinking"),
    [(_HAIKU, "adaptive"), (_HAIKU, None), (_SONNET, None), (_SONNET, "adaptive")],
    ids=["explicit adaptive", "unset on a model that thinks by default", "sonnet unset", "sonnet explicit"],
)
async def test_a_listed_endpoint_that_thinks_sends_drop_block_on_the_beta_namespace(settings, model, thinking):
    body, headers, url = await _sent(settings, model=model, thinking=thinking)

    assert body["thinking"] == _DROP_BLOCK
    assert headers["anthropic-beta"] == _BETA
    assert url.path == "/v1/messages"
    assert url.params.get("beta") == "true"


@pytest.mark.parametrize(
    ("model", "thinking", "expected"),
    [
        (_HAIKU, "disabled", {"type": "disabled"}),
        (_SONNET, "disabled", {"type": "between_tools"}),
        (_OPUS_4_6, None, None),
        (_OPUS_4_6, "adaptive", {"type": "adaptive"}),
        (_PROXY_MODEL, None, None),
        (_PROXY_MODEL, "adaptive", None),
    ],
    ids=[
        "disabled",
        "sonnet between_tools",
        "off by default",
        "earlier model, adaptive",
        "unknown model",
        "unknown model, adaptive",
    ],
)
async def test_a_request_that_does_not_think_carries_no_beta_and_no_block_binding(settings, model, thinking, expected):
    body, headers, url = await _sent(settings, model=model, thinking=thinking)

    assert body.get("thinking") == expected
    assert "anthropic-beta" not in headers
    assert url.params.get("beta") is None


async def test_an_endpoint_outside_the_list_is_sent_the_plain_request(settings):
    unlisted = settings.model_copy(update={"server_compaction_providers": ["somewhere-else"]})

    body, headers, url = await _sent(unlisted, model=_HAIKU, thinking="adaptive")

    assert body["thinking"] == {"type": "adaptive"}
    assert "anthropic-beta" not in headers
    assert url.params.get("beta") is None


async def test_a_compatible_endpoint_in_the_default_list_gets_the_betas(settings):
    foundry = settings.model_copy(
        update={"anthropic_compatible_provider": "foundry", "anthropic_compatible_base_url": "http://foundry.test"}
    )

    body, headers, _url = await _sent(foundry, model=_HAIKU, thinking="adaptive", provider="foundry")

    assert body["thinking"] == _DROP_BLOCK
    assert headers["anthropic-beta"] == _BETA


async def test_the_strict_flag_sends_error_instead_of_drop_block(settings):
    strict = settings.model_copy(update={"strict_thinking_binding": True})

    body, headers, _url = await _sent(strict, model=_HAIKU, thinking="adaptive")

    assert body["thinking"] == {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "error"}}
    assert headers["anthropic-beta"] == _BETA


_DROPS = {
    "input_transformations": [
        {"type": "thinking_dropped", "path": "messages.1.content.0", "reason": "prefix_binding_mismatch"},
        {"type": "thinking_dropped", "path": "messages.3.content.2", "reason": "model_binding_mismatch"},
        {"type": "not_a_kind_pincer_knows", "path": "messages.5.content.0", "reason": "whatever"},
    ]
}


async def _turn(agent: Agent, entry: str) -> str:
    """One turn through an entry point; returns the session key it ran in."""
    if entry == "chat":
        await agent.handle_message("user1", "test", "Hi", memory=False)
        return "test:user1"
    if entry == "stream":
        _chunks = [c async for c in agent.handle_message_stream("user1", "web", "Hi", memory=False)]
        return "web:user1"
    _chunks = [c async for c in agent.stream_voice_turn("caller1", "voice", "Hallo")]
    return "voice:caller1"  # not a phone number: the log masks those


@pytest.mark.parametrize("entry", ["chat", "stream", "voice"])
async def test_a_dropped_thinking_block_is_logged_as_a_warning_with_session_and_path_only(
    settings, session_manager, cost_tracker, registry, caplog, entry
):
    settings.voice_turn_model = ""
    settings.voice_max_response_tokens = 220
    script = [([thinking(_THOUGHT, _SIGNATURE), text("Hello there.")], "end_turn", {}, _DROPS)]
    async with ScriptedAnthropic(settings, script) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry)
        with caplog.at_level(logging.DEBUG, logger="pincer"):
            session_key = await _turn(agent, entry)

    pincer_records = [r for r in caplog.records if r.name.startswith("pincer")]
    assert pincer_records, "precondition: the pincer loggers were captured"
    warnings = [r.getMessage() for r in pincer_records if r.levelno == logging.WARNING and "hinking" in r.getMessage()]
    prefix = f"Thinking block dropped by the API: session={session_key}"
    assert warnings == [
        f"{prefix} path=messages.1.content.0 reason=prefix_binding_mismatch",
        f"{prefix} path=messages.3.content.2 reason=model_binding_mismatch",
    ]
    for record in pincer_records:
        assert _THOUGHT not in record.getMessage()
        assert _SIGNATURE not in record.getMessage()


async def test_a_headless_run_logs_a_dropped_thinking_block_too(
    settings, session_manager, cost_tracker, registry, caplog
):
    script = [([text("Done.")], "end_turn", {}, _DROPS)]
    async with ScriptedAnthropic(settings, script) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry)
        with caplog.at_level(logging.WARNING, logger="pincer"):
            await agent.run_headless("Book the dentist", "user1", "telegram")

    assert [r.getMessage() for r in caplog.records if "hinking" in r.getMessage()] == [
        "Thinking block dropped by the API: session=headless path=messages.1.content.0 reason=prefix_binding_mismatch",
        "Thinking block dropped by the API: session=headless path=messages.3.content.2 reason=model_binding_mismatch",
    ]


async def test_raw_output_shows_in_no_repr_and_no_log_line(settings, session_manager, cost_tracker, registry, caplog):
    responses: list[Any] = []
    async with ScriptedAnthropic(settings, _TOOL_TURN[:2]) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry)
        with caplog.at_level(logging.DEBUG, logger="pincer"):
            real_complete = llm.provider.complete

            async def complete(*args, **kwargs):
                responses.append(await real_complete(*args, **kwargs))
                return responses[-1]

            llm.provider.complete = complete  # type: ignore[method-assign]
            await agent.handle_message("user1", "test", "Book the dentist", memory=False)

    session = await session_manager.get_or_create("user1", "test")
    assert any(_THOUGHT in json.dumps(m.raw) for m in session.messages)  # precondition: the thinking was kept
    assert responses and any(_THOUGHT in json.dumps(r.raw) for r in responses)  # precondition: and returned
    for shown in (*(repr(m) for m in session.messages), *(repr(r) for r in responses), repr(session)):
        assert _THOUGHT not in shown
        assert _REDACTED not in shown
    pincer_records = [r for r in caplog.records if r.name.startswith("pincer")]
    assert pincer_records, "precondition: the pincer loggers were captured"
    for record in pincer_records:
        assert _THOUGHT not in record.getMessage()
        assert _REDACTED not in record.getMessage()


async def test_a_reply_extended_by_the_onboarding_question_is_saved_without_the_models_raw_output(
    settings, mock_llm, session_manager, cost_tracker, registry
):
    """The saved text is no longer what the model wrote, so its output is not replayed with it."""
    raw = [{"type": "text", "text": "Hello."}]
    mock_llm.complete.return_value = LLMResponse(
        content="Hello.", raw=raw, wire="anthropic:test", model="test-model", stop_reason="end_turn"
    )
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, registry)

    first = await agent.handle_message("user-fresh", "test", "Hi!")
    second = await agent.handle_message("user-fresh", "test", "Hi again")

    assert first.text.endswith(ONBOARDING_QUESTION_EN)  # precondition: the question was added
    assert second.text == "Hello."  # and only once
    session = await session_manager.get_or_create("user-fresh", "test")
    replies = [m for m in session.messages if m.role is MessageRole.ASSISTANT]
    assert [(m.content.endswith(ONBOARDING_QUESTION_EN), m.raw, m.wire) for m in replies] == [
        (True, [], ""),
        (False, raw, "anthropic:test"),
    ]


async def test_a_voice_reply_cut_off_at_max_tokens_is_saved_without_its_raw_output(
    settings, session_manager, cost_tracker, registry
):
    settings.voice_turn_model = ""
    settings.voice_max_response_tokens = 220
    script = [
        ([thinking(_THOUGHT, _SIGNATURE), text("Der Termin ist am")], "max_tokens"),
        ([text("Gern.")], "end_turn"),
    ]
    async with ScriptedAnthropic(settings, script) as llm:
        agent = Agent(settings, llm.provider, session_manager, cost_tracker, registry)
        first = [c async for c in agent.stream_voice_turn("+4930111222", "voice", "Wann?")]
        _second = [c async for c in agent.stream_voice_turn("+4930111222", "voice", "Danke")]

    assert first[-1].content == "Der Termin ist am"  # precondition: the cut-off text was spoken and kept
    assert _assistant_contents(llm.requests[1]) == ["Der Termin ist am"]


async def test_a_budget_refused_turn_leaves_the_saved_history_and_its_thinking_untouched(
    settings, mock_llm, session_manager, registry, tmp_path
):
    from pincer.config.pricing import TokenUsage, load_price_table
    from pincer.db.engine import get_database_url
    from pincer.llm.cost_tracker import CostTracker
    from pincer.services.costs import CostService

    history = [
        LLMMessage(role=MessageRole.USER, content="Hi"),
        LLMMessage(
            role=MessageRole.ASSISTANT,
            content="Hello there.",
            raw=[_THOUGHT_BLOCK, {"type": "text", "text": "Hello there."}],
            wire="anthropic:test",
        ),
    ]
    session = await session_manager.get_or_create("user1", "test")
    for message in history:
        await session_manager.add_message(session, message)
    before = [m.to_dict() for m in session.messages]
    assert any(m["raw"] for m in before[1:])  # precondition: the history holds thinking

    # Today's spend is exactly the budget: test-fixture-model costs $7 per MTok, 1,000 tokens = $0.007
    await CostService(get_database_url(tmp_path / "test.db"), prices=load_price_table()).record(
        "anthropic", "test-fixture-model", TokenUsage(input_tokens=1_000)
    )
    tracker = CostTracker(tmp_path / "test.db", daily_budget=0.007, prices=load_price_table())
    await tracker.initialize()
    try:
        agent = Agent(
            settings.model_copy(update={"daily_budget_usd": 0.007}), mock_llm, session_manager, tracker, registry
        )
        reply = await agent.handle_message("user1", "test", "Again", memory=False)
    finally:
        await tracker.close()

    assert reply.budget_reached  # precondition: refused
    assert mock_llm.complete.await_count == 0
    assert [m.to_dict() for m in session.messages] == before
    row = await session_manager._store.load("test:user1", user_id="user1", channel="test", legacy_fallback=False)
    assert row is not None and json.loads(row["messages"]) == before
