"""Endpoints detect what works (issue #358, slice 10).

An endpoint that answers with one of a few exact "not supported" shapes loses that feature for the
process and the same request is retried in the fallback mode. Real SDK error objects and clients.
"""

from __future__ import annotations

import asyncio
import json
import logging

import anthropic
import httpx
import openai
import pytest
from anthropic_sse import Reject, ScriptedAnthropic, text
from openai_wire import ScriptedOpenAI, Status, Turn, message_item, reasoning_item
from pydantic import SecretStr

from pincer.exceptions import LLMError
from pincer.llm.base import LLMMessage, MessageRole
from pincer.llm.capabilities import SWITCHES, Feature, endpoint_capabilities, reasoning_wire, unsupported_features
from pincer.llm.model_params import ReasoningWire

_BASE = "https://api.example.test/v1"
_PREFIX_CHECK = (
    "messages.1.content.0: Invalid `signature` in `thinking` block. The block is bound to a different "
    'conversation. Remove the block, or set `thinking.block_binding.prefix_mismatch_behavior` to "drop_block".'
)


def _openai(status: int, path: str, body: object = None) -> openai.APIStatusError:
    request = httpx.Request("POST", f"{_BASE}{path}")
    return openai.APIStatusError(f"{status}", response=httpx.Response(status, request=request), body=body)


def _anthropic(status: int, message: str, error_type: str = "invalid_request_error") -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.example.test/v1/messages")
    body = {"type": "error", "error": {"type": error_type, "message": message}}
    cls = anthropic.BadRequestError if status == 400 else anthropic.APIStatusError
    return cls(f"Error code: {status} - {body}", response=httpx.Response(status, request=request), body=body)


def _beta(*names: str) -> str:
    listed = ", ".join(f"`{name}`" for name in names)
    return (
        f"Unexpected value(s) {listed} for the `anthropic-beta` header. Please consult our documentation at "
        "platform.claude.com/docs or try again without the header."
    )


_NOTHING: frozenset[Feature] = frozenset()
_ROWS: list[tuple[str, BaseException, frozenset[Feature]]] = [
    ("openai 404 on /responses", _openai(404, "/responses"), frozenset({Feature.RESPONSES_API})),
    ("openai 405 on /responses", _openai(405, "/responses"), frozenset({Feature.RESPONSES_API})),
    ("openai 501 on /responses", _openai(501, "/responses", "Not Implemented"), frozenset({Feature.RESPONSES_API})),
    ("azure path", _openai(404, "/openai/v1/responses"), frozenset({Feature.RESPONSES_API})),
    ("openai 404 on /responses/compact", _openai(404, "/responses/compact"), frozenset({Feature.RESPONSES_COMPACT})),
    ("405 on /responses/compact", _openai(405, "/responses/compact"), _NOTHING),
    ("501 on /responses/compact", _openai(501, "/responses/compact"), _NOTHING),
    ("404 on /chat/completions", _openai(404, "/chat/completions"), _NOTHING),
    ("404 model_not_found", _openai(404, "/responses", {"code": "model_not_found", "message": "no model"}), _NOTHING),
    ("404 DeploymentNotFound", _openai(404, "/responses", {"code": "DeploymentNotFound"}), _NOTHING),
    ("openai 429 on /responses", _openai(429, "/responses"), _NOTHING),
    ("openai 500 on /responses", _openai(500, "/responses"), _NOTHING),
    ("openai 503 on /responses", _openai(503, "/responses"), _NOTHING),
    ("openai 400 on /responses", _openai(400, "/responses", {"message": "bad"}), _NOTHING),
    ("openai 404 on /responses/compact, 500", _openai(500, "/responses/compact"), _NOTHING),
    ("beta header: compaction", _anthropic(400, _beta("compact-2026-09-04")), frozenset({Feature.SERVER_COMPACTION})),
    (
        "beta header: binding",
        _anthropic(400, _beta("thinking-binding-controls-2026-08-01")),
        frozenset({Feature.DROP_BLOCK}),
    ),
    (
        "beta header: both",
        _anthropic(400, _beta("compact-2026-09-04", "thinking-binding-controls-2026-08-01")),
        frozenset({Feature.SERVER_COMPACTION, Feature.DROP_BLOCK}),
    ),
    ("beta header: another beta", _anthropic(400, _beta("invalid-beta-name")), _NOTHING),
    (
        "block_binding extra input",
        _anthropic(400, "thinking.block_binding: Extra inputs are not permitted"),
        frozenset({Feature.DROP_BLOCK}),
    ),
    (
        "compaction extra input",
        _anthropic(400, "compaction: Extra inputs are not permitted"),
        frozenset({Feature.SERVER_COMPACTION}),
    ),
    ("another extra input", _anthropic(400, "foo: Extra inputs are not permitted"), _NOTHING),
    ("the prefix-check 400 names block_binding", _anthropic(400, _PREFIX_CHECK), _NOTHING),
    ("compaction_nothing_to_summarize", _anthropic(400, "compaction_nothing_to_summarize"), _NOTHING),
    ("an unrelated 400", _anthropic(400, "max_tokens: must be greater than 0"), _NOTHING),
    ("anthropic 429", _anthropic(429, _beta("compact-2026-09-04"), "rate_limit_error"), _NOTHING),
    ("anthropic 500", _anthropic(500, "compaction: Extra inputs are not permitted", "api_error"), _NOTHING),
    ("anthropic 503", _anthropic(503, _beta("compact-2026-09-04"), "api_error"), _NOTHING),
    ("anthropic 529", _anthropic(529, _beta("compact-2026-09-04"), "overloaded_error"), _NOTHING),
    ("not an API error", RuntimeError("compaction: Extra inputs are not permitted"), _NOTHING),
]


@pytest.mark.parametrize(("error", "expected"), [row[1:] for row in _ROWS], ids=[row[0] for row in _ROWS])
def test_only_the_exact_shapes_prove_a_feature_unsupported(error, expected):
    assert unsupported_features(error) == expected


# ── OpenAI-wire endpoints: no /responses ─────────────────────────────────────

_NO_RESPONSES_LOG = (
    "Endpoint openai answered 404 on /responses: it has no Responses API, so it uses Chat Completions from now "
    "on (until restart); reasoning is no longer kept across turns on it"
)


@pytest.fixture
def openai_settings(settings):
    return settings.model_copy(
        update={
            "default_provider": "openai",
            "openai_model": "gpt-5.2",
            "openai_api_key": SecretStr("sk-test"),
            "reasoning_effort": "",
            "thinking": "",
            "max_tokens": 4096,
            "temperature": 0.5,
        }
    )


def _hi() -> list[LLMMessage]:
    return [LLMMessage(role=MessageRole.USER, content="hi")]


async def test_a_404_on_responses_retries_the_same_call_on_chat_completions_and_remembers_it(openai_settings, caplog):
    caplog.set_level(logging.WARNING, logger="pincer.llm.capabilities")
    llm = ScriptedOpenAI(openai_settings, [404, "first", "second"])
    history = [
        LLMMessage(role=MessageRole.USER, content="hi"),
        LLMMessage(
            role=MessageRole.ASSISTANT,
            content="hello",
            raw=[reasoning_item("rs_1", "ENC-1"), message_item("hello")],
            wire="openai:gpt-5.2",
        ),
        LLMMessage(role=MessageRole.USER, content="and?"),
    ]
    assert endpoint_capabilities(openai_settings, "openai").responses_api  # precondition: listed
    assert reasoning_wire(openai_settings, "openai") is ReasoningWire.OPENAI_RESPONSES

    first = await llm.provider.complete(history)
    second = await llm.provider.complete(history)

    assert (first.content, second.content) == ("first", "second")
    assert llm.paths == ["/v1/responses", "/v1/chat/completions", "/v1/chat/completions"]
    assert "ENC-1" not in json.dumps(llm.requests[1])  # the Responses-saved history is rebuilt for Chat
    assert [r.getMessage() for r in caplog.records] == [_NO_RESPONSES_LOG]
    assert reasoning_wire(openai_settings, "openai") is ReasoningWire.OPENAI
    assert not endpoint_capabilities(openai_settings, "openai").responses_compact
    assert endpoint_capabilities(openai_settings, "azure").responses_api  # another endpoint is not affected


@pytest.mark.parametrize("status", [400, 500, 503])
async def test_other_errors_on_responses_flip_nothing_and_are_not_retried(openai_settings, status):
    llm = ScriptedOpenAI(openai_settings, [status])
    with pytest.raises(LLMError):
        await llm.provider.complete(_hi())

    assert llm.paths == ["/v1/responses"]
    assert endpoint_capabilities(openai_settings, "openai").responses_api
    assert SWITCHES.listed() == []


async def test_two_calls_that_both_get_the_404_both_succeed_on_chat_completions_with_one_warning(
    openai_settings, caplog
):
    caplog.set_level(logging.WARNING, logger="pincer.llm.capabilities")
    gate = asyncio.Event()
    llm = ScriptedOpenAI(openai_settings, [Status(404, gate=gate), Status(404, gate=gate), "A", "B"])
    calls = [asyncio.create_task(llm.provider.complete(_hi())) for _ in range(2)]
    for _ in range(500):
        if len(llm.requests) == 2:
            break
        await asyncio.sleep(0.01)
    assert llm.paths == ["/v1/responses", "/v1/responses"]  # precondition: both are in flight when the 404 arrives

    gate.set()
    results = await asyncio.gather(*calls)

    assert sorted(r.content for r in results) == ["A", "B"]
    assert sorted(llm.paths) == ["/v1/chat/completions"] * 2 + ["/v1/responses"] * 2
    assert [r.getMessage() for r in caplog.records] == [_NO_RESPONSES_LOG]


async def test_a_streamed_turn_whose_connection_gets_the_404_streams_on_chat_completions(openai_settings, caplog):
    caplog.set_level(logging.WARNING, logger="pincer.llm.capabilities")
    llm = ScriptedOpenAI(openai_settings, [Status(404), Turn(text=["Hel", "lo"])])
    events = [event async for event in llm.provider.stream_turn(_hi())]

    assert llm.paths == ["/v1/responses", "/v1/chat/completions"]
    assert [e.text for e in events[:-1]] == ["Hel", "lo"]
    assert events[-1].response is not None and events[-1].response.content == "Hello"
    assert [r.getMessage() for r in caplog.records] == [_NO_RESPONSES_LOG]


async def test_a_streamed_turn_with_another_error_flips_nothing(openai_settings):
    llm = ScriptedOpenAI(openai_settings, [Status(500)])
    with pytest.raises(LLMError):
        [event async for event in llm.provider.stream_turn(_hi())]

    assert llm.paths == ["/v1/responses"]
    assert SWITCHES.listed() == []


# ── gpt-6 on the fallback ────────────────────────────────────────────────────

_TOOLS = [{"name": "lookup", "description": "Look something up", "input_schema": {"type": "object"}}]


@pytest.mark.parametrize("effort", ["", "high"])
async def test_a_gpt_6_model_with_an_off_level_falls_back_with_reasoning_off_for_tools(openai_settings, effort):
    settings = openai_settings.model_copy(update={"openai_model": "gpt-6.1-terra", "reasoning_effort": effort})
    llm = ScriptedOpenAI(settings, [404, "ok"], model="gpt-6.1-terra")
    result = await llm.provider.complete(_hi(), tools=_TOOLS)

    assert result.content == "ok" and llm.paths == ["/v1/responses", "/v1/chat/completions"]
    assert llm.requests[0].get("reasoning") == ({"effort": effort} if effort else None)  # precondition
    assert llm.requests[1]["reasoning_effort"] == "none"  # Chat Completions takes gpt-6 tools only with it off


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6.1-sol"])
async def test_a_gpt_6_model_with_no_off_level_cannot_use_tools_on_the_fallback(openai_settings, caplog, model):
    caplog.set_level(logging.WARNING, logger="pincer.llm")
    settings = openai_settings.model_copy(update={"openai_model": model})
    llm = ScriptedOpenAI(settings, [404, "unused"], model=model)
    expected = f"openai: {model} needs the Responses API to use tools, and this endpoint has no /responses (HTTP 404)"

    for _ in range(2):
        with pytest.raises(LLMError, match=expected.replace("(", r"\(").replace(")", r"\)")) as raised:
            await llm.provider.complete(_hi(), tools=_TOOLS)
        assert not raised.value.retryable

    assert llm.paths == ["/v1/responses"]  # nothing was sent to Chat Completions, now or the second time
    warnings = [r.getMessage() for r in caplog.records if r.name == "pincer.llm.openai_common"]
    assert warnings == [
        f"Endpoint openai has no /responses (HTTP 404) and {model} cannot use tools on Chat Completions: its "
        "requests with tools fail until restart"
    ]


async def test_a_gpt_6_model_with_no_off_level_still_answers_without_tools_on_the_fallback(openai_settings):
    settings = openai_settings.model_copy(update={"openai_model": "gpt-6-astra"})
    llm = ScriptedOpenAI(settings, [404, "ok"], model="gpt-6-astra")
    result = await llm.provider.complete(_hi())

    assert result.content == "ok" and llm.paths == ["/v1/responses", "/v1/chat/completions"]


# ── /responses/compact: no endpoint, the summary model compacts ──────────────

_NO_COMPACT_LOG = (
    "Endpoint openai answered 404 on /responses/compact: it has no server compaction, so its conversations are "
    "compacted by the summary model from now on (until restart)"
)
_BIG = 40_000


def _switch_logs(caplog) -> list[str]:
    """The WARNINGs about a feature an endpoint lost."""
    return [r.getMessage() for r in caplog.records if r.name == "pincer.llm.capabilities"]


class _Summarizer:
    """The summary model of the client strategy."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, **_kwargs):
        from pincer.llm.base import LLMResponse

        self.calls += 1
        return LLMResponse(content="SUMMARY-OF-THE-TALK", provider="openai", request_model="test-fixture-model")


async def _long_session(sessions, user: str):
    session = await sessions.get_or_create(user, "test")
    for index in range(10):
        role = MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT
        await sessions.add_message(session, LLMMessage(role=role, content=f"message {index} " + "x" * _BIG))
    await sessions.add_message(session, LLMMessage(role=MessageRole.USER, content="turn 1"))
    sent = list(session.messages)
    await sessions.add_message(session, LLMMessage(role=MessageRole.ASSISTANT, content="Reply 1"))
    return session, sent


def _compactor_for(settings, llm, summarizer, costs, sessions):
    from pincer.core.compaction import Compactor, ServerCompaction

    server = ServerCompaction(settings, lambda _name, _model: llm.provider, lambda _name: False)
    return Compactor(
        summarizer,
        "test-fixture-model",
        costs,
        sessions,
        None,
        provider="openai",
        is_free=False,
        at_tokens=80_000,
        to_tokens=50_000,
        server=server,
    )


def _last(sent):
    from pincer.core.compaction import LastRequest, SentRequest

    return LastRequest.measure(100_000, sent, 2_000, request=SentRequest("openai", "gpt-5.2", None, None))


async def test_a_404_on_responses_compact_makes_the_summary_model_compact_at_once_and_from_then_on(
    openai_settings, session_manager, cost_tracker, caplog
):
    from pincer.core.compaction import CompactionOutcome, CompactionStrategy, pick_strategy

    caplog.set_level(logging.WARNING, logger="pincer.llm.capabilities")
    llm = ScriptedOpenAI(openai_settings, [Status(404)])
    summarizer = _Summarizer()
    compactor = _compactor_for(openai_settings, llm, summarizer, cost_tracker, session_manager)
    first, sent = await _long_session(session_manager, "user1")
    assert pick_strategy(openai_settings, "openai", llm.provider) is CompactionStrategy.OPENAI_SERVER  # precondition

    assert await compactor.compact(first, _last(sent)) is CompactionOutcome.COMPACTED

    assert llm.paths == ["/v1/responses/compact"] and summarizer.calls == 1  # asked once, then the summary model
    assert first.messages[0].content == "[Previous conversation summary]\nSUMMARY-OF-THE-TALK"
    assert pick_strategy(openai_settings, "openai", llm.provider) is CompactionStrategy.CLIENT_SUMMARY
    assert endpoint_capabilities(openai_settings, "openai").responses_api  # /responses itself still works
    assert _switch_logs(caplog) == [_NO_COMPACT_LOG]

    second, sent = await _long_session(session_manager, "user2")
    assert await compactor.compact(second, _last(sent)) is CompactionOutcome.COMPACTED
    assert llm.paths == ["/v1/responses/compact"] and summarizer.calls == 2  # no second request to the endpoint
    assert len(_switch_logs(caplog)) == 1


async def test_another_error_on_responses_compact_flips_nothing(openai_settings, session_manager, cost_tracker):
    from pincer.core.compaction import CompactionOutcome

    llm = ScriptedOpenAI(openai_settings, [Status(500)])
    summarizer = _Summarizer()
    compactor = _compactor_for(openai_settings, llm, summarizer, cost_tracker, session_manager)
    session, sent = await _long_session(session_manager, "user1")
    before = list(session.messages)

    assert await compactor.compact(session, _last(sent)) is CompactionOutcome.FAILED

    assert session.messages == before and summarizer.calls == 0
    assert endpoint_capabilities(openai_settings, "openai").responses_compact
    assert SWITCHES.listed() == []


# ── Anthropic-wire endpoints: a rejected beta ────────────────────────────────

_HAIKU = "claude-haiku-5-5"
_BINDING_LOG = (
    "Endpoint anthropic rejected the thinking-binding beta (HTTP 400): drop_block is off for it from now on "
    "(until restart)"
)
_COMPACTION_LOG = (
    "Endpoint anthropic rejected the compaction beta (HTTP 400): server compaction is off for it, so its "
    "conversations are compacted by the summary model and stored compaction blocks are sent as text, from now "
    "on (until restart)"
)
_BETA_HEADER = "anthropic-beta"


@pytest.mark.parametrize("path", ["complete", "stream"])
async def test_a_rejected_binding_beta_is_retried_without_it_and_remembered(settings, caplog, path):
    caplog.set_level(logging.WARNING, logger="pincer.llm.capabilities")
    script = [
        Reject(400, _beta("thinking-binding-controls-2026-08-01")),
        ([text("ok")], "end_turn"),
        ([text("2")], "end_turn"),
    ]
    async with ScriptedAnthropic(settings, script) as llm:
        for _ in range(2):
            if path == "complete":
                await llm.provider.complete(_hi(), model=_HAIKU)
            else:
                [e async for e in llm.provider.stream_turn(_hi(), model=_HAIKU)]

    first, retry, second = llm.requests
    assert "block_binding" in json.dumps(first["thinking"]) and llm.headers[0][_BETA_HEADER]  # precondition
    assert "block_binding" not in json.dumps(retry.get("thinking")) and _BETA_HEADER not in llm.headers[1]
    assert "block_binding" not in json.dumps(second.get("thinking")) and _BETA_HEADER not in llm.headers[2]
    assert not endpoint_capabilities(settings, "anthropic").drop_block
    assert [r.getMessage() for r in caplog.records] == [_BINDING_LOG]


@pytest.mark.parametrize(
    "message",
    [
        _PREFIX_CHECK,
        "max_tokens: must be greater than 0",
        "compaction_nothing_to_summarize",
        _beta("invalid-beta-name"),
    ],
    ids=["the prefix-check 400", "an unrelated 400", "a compaction code", "an unknown beta"],
)
async def test_other_400s_flip_nothing_and_are_not_retried(settings, message):
    async with ScriptedAnthropic(settings, [Reject(400, message)]) as llm:
        with pytest.raises(LLMError):
            await llm.provider.complete(_hi(), model=_HAIKU)

    assert len(llm.requests) == 1
    assert endpoint_capabilities(settings, "anthropic").drop_block and SWITCHES.listed() == []


def _stored_block_history() -> list[LLMMessage]:
    block = {"type": "compaction", "content": "S-UMMARY", "signature": "SIG"}
    summary = LLMMessage(
        role=MessageRole.SYSTEM,
        content="[Previous conversation summary]\nS-UMMARY",
        raw=[block],
        wire=f"anthropic:{_HAIKU}",
    )
    return [summary, LLMMessage(role=MessageRole.ASSISTANT, content="Reply 1"), *_hi()]


async def test_a_rejected_compaction_beta_sends_the_stored_block_as_text_from_then_on(settings, caplog):
    caplog.set_level(logging.WARNING, logger="pincer.llm.capabilities")
    script = [Reject(400, _beta("compact-2026-09-04")), ([text("ok")], "end_turn"), ([text("2")], "end_turn")]
    async with ScriptedAnthropic(settings, script) as llm:
        for _ in range(2):
            await llm.provider.complete(_stored_block_history(), model=_HAIKU)

    first, retry, second = llm.requests
    assert first["messages"][0]["content"][0]["type"] == "compaction"  # precondition: the block went out
    assert "compact-2026-09-04" in llm.headers[0][_BETA_HEADER]
    text_block = {"type": "text", "text": "[Previous conversation summary]\nS-UMMARY"}
    for request, headers in ((retry, llm.headers[1]), (second, llm.headers[2])):
        assert request["messages"][0] == {"role": "user", "content": [text_block]}
        assert "compact-2026-09-04" not in headers.get(_BETA_HEADER, "")
    assert [r.getMessage() for r in caplog.records] == [_COMPACTION_LOG]


async def test_a_400_on_the_compaction_call_makes_the_summary_model_compact_at_once(
    settings, session_manager, cost_tracker, caplog
):
    from pincer.core.compaction import (
        CompactionOutcome,
        CompactionStrategy,
        Compactor,
        LastRequest,
        SentRequest,
        ServerCompaction,
        pick_strategy,
    )

    caplog.set_level(logging.WARNING, logger="pincer.llm.capabilities")
    summarizer = _Summarizer()
    session, sent = await _long_session(session_manager, "user1")
    async with ScriptedAnthropic(settings, [Reject(400, "compaction: Extra inputs are not permitted")]) as llm:
        server = ServerCompaction(settings, lambda _name, _model: llm.provider, lambda _name: False)
        compactor = Compactor(
            summarizer,
            "test-fixture-model",
            cost_tracker,
            session_manager,
            None,
            provider="anthropic",
            is_free=False,
            at_tokens=80_000,
            to_tokens=50_000,
            server=server,
        )
        last = LastRequest.measure(100_000, sent, 2_000, request=SentRequest("anthropic", _HAIKU, None, None))
        assert pick_strategy(settings, "anthropic", llm.provider) is CompactionStrategy.ANTHROPIC_SERVER  # precondition

        assert await compactor.compact(session, last) is CompactionOutcome.COMPACTED
        assert pick_strategy(settings, "anthropic", llm.provider) is CompactionStrategy.CLIENT_SUMMARY

    assert len(llm.requests) == 1 and summarizer.calls == 1
    assert session.messages[0].content == "[Previous conversation summary]\nSUMMARY-OF-THE-TALK"
    assert _switch_logs(caplog) == [_COMPACTION_LOG]


# ── what is remembered ───────────────────────────────────────────────────────


def test_a_switch_is_logged_once_per_endpoint_and_feature_and_touches_only_that_endpoint(settings, caplog):
    caplog.set_level(logging.WARNING, logger="pincer.llm.capabilities")
    settings.anthropic_compatible_provider = "foundry"

    assert SWITCHES.switch_off("foundry", Feature.DROP_BLOCK, 400) is True
    assert SWITCHES.switch_off("foundry", Feature.DROP_BLOCK, 400) is False
    assert SWITCHES.switch_off("foundry", Feature.SERVER_COMPACTION, 400) is True

    assert len(_switch_logs(caplog)) == 2  # one line per feature, none for the repeat
    foundry, direct = endpoint_capabilities(settings, "foundry"), endpoint_capabilities(settings, "anthropic")
    assert (foundry.drop_block, foundry.server_compaction) == (False, False)
    assert (direct.drop_block, direct.server_compaction) == (True, True)  # precondition: the others still listed
    assert [(s.endpoint, s.feature) for s in SWITCHES.listed()] == [
        ("foundry", Feature.DROP_BLOCK),
        ("foundry", Feature.SERVER_COMPACTION),
    ]
    SWITCHES.reset()
    assert endpoint_capabilities(settings, "foundry").drop_block and SWITCHES.listed() == []
