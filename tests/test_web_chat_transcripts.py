"""Web chat transcripts on the session tables (issue #243).

A conversation's messages are rows of `pincer_session_messages`; the ones compaction, a cut or a clear
take out of the model's history are retired, not deleted, so the transcript people read is complete.
The read endpoints serve it, scoped to the caller; a conversation id another identity owns is refused.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

from pincer.core.agent import Agent, StreamEventType
from pincer.core.session import SessionManager, conversation_key
from pincer.llm.base import ImageContent, LLMMessage, LLMResponse, MessageRole, StreamTurnEvent, ToolCall
from pincer.services.sessions import SessionService, get_session_service

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi.testclient import TestClient

ALICE = "11111111-1111-4111-8111-111111111111"
BOB = "44444444-4444-4444-8444-444444444444"
CHAT_A = "22222222-2222-4222-8222-222222222222"
CHAT_B = "33333333-3333-4333-8333-333333333333"
ANY_ID = "00000000-0000-0000-0000-000000000000"


def _reply(text: str) -> LLMResponse:
    return LLMResponse(content=text, model="test-model", input_tokens=10, output_tokens=5, stop_reason="end_turn")


def _echo_llm(llm: Any) -> None:
    """Each streamed turn answers `reply to <the user's last message>`, in two chunks."""

    async def stream_turn(*args: Any, **kwargs: Any) -> AsyncIterator[StreamTurnEvent]:
        said = next(m.content for m in reversed(kwargs["messages"]) if m.role == MessageRole.USER)
        yield StreamTurnEvent(text="reply to ")
        yield StreamTurnEvent(text=said)
        yield StreamTurnEvent(response=_reply(f"reply to {said}"))

    llm.stream_turn = stream_turn


async def _stream(agent: Agent, user_id: str, text: str, conversation: str) -> str:
    key = conversation_key(user_id, "web", conversation)
    chunks = [c async for c in agent.handle_message_stream(user_id, "web", text, session_key=key)]
    assert chunks[-1].type == StreamEventType.DONE
    return chunks[-1].content


def _client_on(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The API (auth off: `X-Pincer-User` names the caller) with a mocked agent, reading sessions from
    `db_path`."""
    from fastapi.testclient import TestClient

    from pincer.api.server import create_app
    from pincer.config import get_settings_relaxed
    from pincer.core.agent import AgentResponse

    monkeypatch.chdir(db_path.parent)  # away from the developer's .env
    get_settings_relaxed.cache_clear()
    try:
        app = create_app()
    finally:
        get_settings_relaxed.cache_clear()
    agent = AsyncMock()
    agent.handle_message = AsyncMock(return_value=AgentResponse(text="hi back", cost_usd=0.0, model="m"))
    agent.stream_calls = []

    async def stream(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        agent.stream_calls.append((args, kwargs))
        if False:
            yield None

    agent.handle_message_stream = stream
    app.state.agent = agent
    client = TestClient(app)

    async def service() -> SessionService:
        return await SessionService.for_path(db_path)

    client.app.dependency_overrides[get_session_service] = service
    return client


def _transcript(client: TestClient, user_id: str, conversation: str, *, limit: int) -> list[dict[str, Any]]:
    """Every message of the conversation, read newest page first and back to the oldest."""
    pages: list[list[dict[str, Any]]] = []
    before: str | None = None
    while True:
        params: dict[str, Any] = {"limit": limit} | ({"before": before} if before else {})
        r = client.get(
            f"/api/chat/conversations/{conversation}/messages", params=params, headers={"X-Pincer-User": user_id}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        pages.append(body["messages"])
        if not body["has_more"]:
            assert body["next_before"] is None
            break
        before = body["next_before"]
    return [message for page in reversed(pages) for message in page]


# ── the agent ────────────────────────────────────────────────────────


async def test_thirty_streamed_exchanges_come_back_complete_after_compaction_and_a_restart(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, tmp_path, monkeypatch
):
    _echo_llm(mock_llm)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    key = conversation_key(ALICE, "web", CHAT_A)

    for n in range(30):
        await _stream(agent, ALICE, f"message {n}", CHAT_A)
        if n % 10 == 9:  # compaction replaces all but the last exchange with a summary
            session = await session_manager.get_or_create(ALICE, "web", session_key=key)
            summary = LLMMessage(role=MessageRole.SYSTEM, content=f"[summary up to {n}]")
            assert await session_manager.compact(session, list(session.messages[:-2]), summary)
    live = await session_manager.get_or_create(ALICE, "web", session_key=key)
    assert len(live.messages) < 60  # precondition: the model's history was compacted

    # A fresh process: nothing cached, the transcript read from the rows
    transcript = _transcript(_client_on(tmp_path / "test.db", monkeypatch), ALICE, CHAT_A, limit=7)

    assert [(m["role"], m["content"]) for m in transcript if m["role"] == "user"] == [
        ("user", f"message {n}") for n in range(30)
    ]
    replies = [m["content"] for m in transcript if m["role"] == "assistant"]
    assert len(replies) == 30
    assert all(reply.startswith(f"reply to message {n}") for n, reply in enumerate(replies))
    assert [m["role"] for m in transcript] == ["user", "assistant"] * 30
    assert not any(m["interrupted"] for m in transcript)
    fresh = SessionManager(tmp_path / "test.db")
    await fresh.initialize()
    reloaded = await fresh.get_or_create(ALICE, "web", session_key=key)
    await fresh.close()
    assert [m.content for m in reloaded.messages] == [m.content for m in live.messages]  # the model's, unchanged


async def test_two_conversations_keep_separate_model_contexts(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    _echo_llm(mock_llm)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    await _stream(agent, ALICE, "my locker code is 4711", CHAT_A)
    await _stream(agent, ALICE, "what is my locker code?", CHAT_B)

    chat_b = await session_manager.get_or_create(ALICE, "web", session_key=conversation_key(ALICE, "web", CHAT_B))
    assert not any("4711" in m.content for m in chat_b.messages)


@pytest.mark.parametrize("how", ["closed", "cancelled"])
async def test_a_reply_cut_short_is_saved_once_with_what_was_streamed(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, tmp_path, monkeypatch, how
):
    never = asyncio.Event()

    async def stream_turn(*args: Any, **kwargs: Any) -> AsyncIterator[StreamTurnEvent]:
        yield StreamTurnEvent(text="Here is the ")
        yield StreamTurnEvent(text="first half")
        await never.wait()  # the client goes away before the rest
        yield StreamTurnEvent(response=_reply("unreachable"))

    mock_llm.stream_turn = stream_turn
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    key = conversation_key(ALICE, "web", CHAT_A)
    stream = agent.handle_message_stream(ALICE, "web", "tell me", session_key=key)
    received: list[str] = []

    async def read() -> None:
        async for chunk in stream:
            received.append(chunk.content)
            if len(received) == 2 and how == "closed":
                return

    if how == "closed":
        await read()
        await stream.aclose()
    else:
        task = asyncio.create_task(read())
        while len(received) < 2:
            await asyncio.sleep(0)
        await asyncio.sleep(0.01)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await stream.aclose()

    transcript = _transcript(_client_on(tmp_path / "test.db", monkeypatch), ALICE, CHAT_A, limit=50)
    assert [(m["role"], m["content"], m["interrupted"]) for m in transcript] == [
        ("user", "tell me", False),
        ("assistant", "Here is the first half", True),
    ]


def _tool_step(text: str = "") -> LLMResponse:
    call = ToolCall(id=f"t{id(text)}", name="no_such_tool", arguments={})
    return LLMResponse(
        content=text, model="test-model", input_tokens=10, output_tokens=5, stop_reason="tool_use", tool_calls=[call]
    )


async def test_a_client_gone_while_the_tool_step_saves_keeps_its_text_once(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, monkeypatch
):
    """The step's message is in the session as soon as `add_message` is called: a cancel while it is
    written must not save its text a second time, as a reply cut short."""

    async def stream_turn(*args: Any, **kwargs: Any) -> AsyncIterator[StreamTurnEvent]:
        yield StreamTurnEvent(text="Let me check")
        yield StreamTurnEvent(response=_tool_step("Let me check"))

    mock_llm.stream_turn = stream_turn
    persist = session_manager._persist
    writing = asyncio.Event()

    async def slow_persist(session: Any, **kwargs: Any) -> None:
        if session.messages[-1].tool_calls and not writing.is_set():
            writing.set()
            await asyncio.Event().wait()  # the client goes away while the tool step is written
        await persist(session, **kwargs)

    monkeypatch.setattr(session_manager, "_persist", slow_persist)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)
    key = conversation_key(ALICE, "web", CHAT_A)

    async def read() -> None:
        async for _chunk in agent.handle_message_stream(ALICE, "web", "check it", session_key=key):
            pass

    task = asyncio.create_task(read())
    await asyncio.wait_for(writing.wait(), 2)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    session = await session_manager.get_or_create(ALICE, "web", session_key=key)
    assistant = [m for m in session.messages if m.role is MessageRole.ASSISTANT]
    assert [(m.content, bool(m.tool_calls), m.is_error) for m in assistant] == [("Let me check", True, False)]


async def test_a_streamed_turn_ending_at_the_iteration_limit_stores_its_exchange(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, tmp_path, monkeypatch
):
    """The canned ending is the reply the client got: saved once, and remembered as the blocking path
    remembers it."""

    async def stream_turn(*args: Any, **kwargs: Any) -> AsyncIterator[StreamTurnEvent]:
        yield StreamTurnEvent(response=_tool_step())

    mock_llm.stream_turn = stream_turn
    memory = AsyncMock()
    memory.search_text.return_value = []
    settings = settings.model_copy(update={"max_tool_iterations": 1})
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry, memory_store=memory)

    done = await _stream(agent, ALICE, "loop forever", CHAT_A)

    assert done == "Max iterations reached."
    exchanges = [c for c in memory.store_memory.await_args_list if c.kwargs.get("category") == "exchange"]
    assert len(exchanges) == 1
    assert "Max iterations reached." in exchanges[0].kwargs["content"]
    transcript = _transcript(_client_on(tmp_path / "test.db", monkeypatch), ALICE, CHAT_A, limit=50)
    assert [(m["role"], m["content"]) for m in transcript] == [
        ("user", "loop forever"),
        ("assistant", "Max iterations reached."),
    ]


async def test_a_turn_the_budget_stops_mid_way_shows_as_cut_short(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, tmp_path, monkeypatch
):
    async def stream_turn(*args: Any, **kwargs: Any) -> AsyncIterator[StreamTurnEvent]:
        yield StreamTurnEvent(response=_tool_step())

    mock_llm.stream_turn = stream_turn
    monkeypatch.setattr(cost_tracker, "budget_reached", AsyncMock(side_effect=[False, True]))
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    await _stream(agent, ALICE, "spend it all", CHAT_A)

    transcript = _transcript(_client_on(tmp_path / "test.db", monkeypatch), ALICE, CHAT_A, limit=50)
    assert [(m["role"], m["interrupted"]) for m in transcript] == [("user", False), ("assistant", True)]
    assert transcript[-1]["content"].startswith("(Stopped before answering")


async def test_a_streamed_turn_stores_its_exchange_as_a_memory(
    settings, mock_llm, session_manager, cost_tracker, tool_registry
):
    """As the blocking path does: most web turns stream, and recall missed them."""
    _echo_llm(mock_llm)
    memory = AsyncMock()
    memory.search_text.return_value = []
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry, memory_store=memory)

    await _stream(agent, ALICE, "remember the blue door", CHAT_A)

    exchanges = [c for c in memory.store_memory.await_args_list if c.kwargs.get("category") == "exchange"]
    assert len(exchanges) == 1
    content = exchanges[0].kwargs["content"]
    assert "remember the blue door" in content
    assert "reply to remember the blue door" in content


async def test_a_streamed_turn_the_budget_refuses_stores_no_exchange(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, monkeypatch
):
    _echo_llm(mock_llm)
    memory = AsyncMock()
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry, memory_store=memory)
    monkeypatch.setattr(cost_tracker, "budget_reached", AsyncMock(return_value=True))

    await _stream(agent, ALICE, "hello", CHAT_A)

    assert not [c for c in memory.store_memory.await_args_list if c.kwargs.get("category") == "exchange"]


# ── the API ──────────────────────────────────────────────────────────


async def _seed(db_path: Path, user_id: str, conversation: str, *texts: str, memory_free: bool = False) -> None:
    manager = SessionManager(db_path)
    await manager.initialize()
    session = await manager.get_or_create(user_id, "web", session_key=conversation_key(user_id, "web", conversation))
    if memory_free:
        session.mark_memory_free()
    for text in texts:
        await manager.add_message(session, LLMMessage(role=MessageRole.USER, content=text))
        await manager.add_message(session, LLMMessage(role=MessageRole.ASSISTANT, content=f"re: {text}"))
    await manager.close()


async def test_another_identity_gets_404_for_someone_else_s_conversation(tmp_path, monkeypatch):
    db = tmp_path / "pincer.db"
    await _seed(db, ALICE, CHAT_A, "private")
    client = _client_on(db, monkeypatch)
    bob = {"X-Pincer-User": BOB}

    assert client.get(f"/api/chat/conversations/{CHAT_A}/messages", headers=bob).status_code == 404
    assert client.get("/api/chat/conversations", headers=bob).json() == {"items": [], "next_cursor": None}
    assert (
        client.post("/api/chat/message", json={"text": "hi", "conversation_id": CHAT_A}, headers=bob).status_code == 404
    )
    r = client.post("/api/chat/stream", json={"text": "hi", "conversation_id": CHAT_A.upper()}, headers=bob)
    assert r.status_code == 404
    client.app.state.agent.handle_message.assert_not_awaited()
    assert client.app.state.agent.stream_calls == []

    # The owner reads and continues it; a fresh id is anybody's first
    alice = {"X-Pincer-User": ALICE}
    assert client.get(f"/api/chat/conversations/{CHAT_A}/messages", headers=alice).status_code == 200
    assert (
        client.post("/api/chat/message", json={"text": "hi", "conversation_id": CHAT_A}, headers=alice).status_code
        == 200
    )
    assert (
        client.post("/api/chat/message", json={"text": "hi", "conversation_id": CHAT_B}, headers=bob).status_code == 200
    )
    # An id nobody has used yet has no transcript to read
    assert client.get(f"/api/chat/conversations/{CHAT_B}/messages", headers=bob).status_code == 404


async def test_the_conversation_list_is_the_caller_s_latest_first_without_memory_free_ones(tmp_path, monkeypatch):
    db = tmp_path / "pincer.db"
    chats = [f"5555555{n}-5555-4555-8555-555555555555" for n in range(5)]
    for n, chat in enumerate(chats):
        await _seed(db, ALICE, chat, f"chat {n} starts here", "and goes on")
    await _seed(db, ALICE, CHAT_A, "summarize the call", memory_free=True)
    await _seed(db, BOB, CHAT_B, "bob's own")
    client = _client_on(db, monkeypatch)
    alice = {"X-Pincer-User": ALICE}

    first = client.get("/api/chat/conversations", params={"limit": 3}, headers=alice).json()
    second = client.get(
        "/api/chat/conversations", params={"limit": 3, "cursor": first["next_cursor"]}, headers=alice
    ).json()

    items = first["items"] + second["items"]
    assert [i["id"] for i in items] == list(reversed(chats))
    assert second["next_cursor"] is None
    assert items[0]["title"] == "chat 4 starts here"
    assert all(i["message_count"] == 4 for i in items)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    "cursor",
    [
        "%%%",
        _b64(f"2026-01-01T00:00:00|{ANY_ID}"),
        _b64(f"0001-01-01T00:00:00+05:00|{ANY_ID}"),
        _b64("2026-01-01T00:00:00+00:00"),
    ],
    ids=["not-base64", "naive-time", "out-of-range-time", "no-separator"],
)
@pytest.mark.parametrize("endpoint", ["list", "transcript"])
async def test_a_cursor_this_api_did_not_hand_out_is_a_422(tmp_path, monkeypatch, cursor, endpoint):
    db = tmp_path / "pincer.db"
    await _seed(db, ALICE, CHAT_A, "hello")
    client = _client_on(db, monkeypatch)

    if endpoint == "list":
        r = client.get("/api/chat/conversations", params={"cursor": cursor}, headers={"X-Pincer-User": ALICE})
    else:
        r = client.get(
            f"/api/chat/conversations/{CHAT_A}/messages", params={"before": cursor}, headers={"X-Pincer-User": ALICE}
        )

    assert r.status_code == 422, r.text


async def test_the_transcript_shows_what_was_said_not_tool_steps_or_summaries(tmp_path, monkeypatch):
    db = tmp_path / "pincer.db"
    manager = SessionManager(db)
    await manager.initialize()
    session = await manager.get_or_create(ALICE, "web", session_key=conversation_key(ALICE, "web", CHAT_A))
    call = ToolCall(id="t1", name="lookup", arguments={})
    for message in (
        LLMMessage(role=MessageRole.USER, content="look it up"),
        LLMMessage(role=MessageRole.ASSISTANT, content="", tool_calls=[call]),
        LLMMessage(role=MessageRole.TOOL_RESULT, content="found", tool_call_id="t1"),
        LLMMessage(role=MessageRole.ASSISTANT, content="It is 42."),
    ):
        await manager.add_message(session, message)
    summary = LLMMessage(role=MessageRole.SYSTEM, content="[summary]")
    assert await manager.compact(session, list(session.messages), summary)
    await manager.close()

    transcript = _transcript(_client_on(db, monkeypatch), ALICE, CHAT_A, limit=50)

    assert [(m["role"], m["content"]) for m in transcript] == [("user", "look it up"), ("assistant", "It is 42.")]


async def test_a_photo_only_message_is_in_the_transcript_and_not_the_title(tmp_path, monkeypatch):
    db = tmp_path / "pincer.db"
    manager = SessionManager(db)
    await manager.initialize()
    session = await manager.get_or_create(ALICE, "web", session_key=conversation_key(ALICE, "web", CHAT_A))
    photo = ImageContent.from_bytes(b"\x89PNG fake", "image/png")
    for message in (
        LLMMessage(role=MessageRole.USER, content="", images=[photo]),
        LLMMessage(role=MessageRole.ASSISTANT, content="A cat."),
        LLMMessage(role=MessageRole.USER, content="what breed?"),
    ):
        await manager.add_message(session, message)
    await manager.close()
    client = _client_on(db, monkeypatch)

    transcript = _transcript(client, ALICE, CHAT_A, limit=50)
    listed = client.get("/api/chat/conversations", headers={"X-Pincer-User": ALICE}).json()["items"]

    assert [(m["role"], m["content"], m["images"]) for m in transcript] == [
        ("user", "", 1),
        ("assistant", "A cat.", 0),
        ("user", "what breed?", 0),
    ]
    assert [(i["title"], i["message_count"]) for i in listed] == [("what breed?", 3)]
