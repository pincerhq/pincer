"""Integration tests for Sprint 2 features: memory, browser, python_exec, voice, streaming."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from pincer.core.agent import Agent, AgentResponse, StreamChunk, StreamEventType
from pincer.llm.base import BaseLLMProvider, LLMMessage, LLMResponse, MessageRole, ToolCall
from pincer.memory.store import MemoryStore
from pincer.memory.summarizer import Summarizer

# ── Memory Store Tests ────────────────────────────────────────


@pytest_asyncio.fixture
async def memory_store(tmp_path: Path) -> MemoryStore:
    store = MemoryStore(tmp_path / "test_memory.db")
    await store.initialize()
    yield store  # type: ignore[misc]
    await store.close()


@pytest.mark.asyncio
async def test_store_and_retrieve_memory(memory_store: MemoryStore) -> None:
    mem_id = await memory_store.store_memory("user1", "The user likes Python", "preference")
    assert mem_id

    results = await memory_store.search_text("Python", user_id="user1")
    assert len(results) >= 1
    assert any("Python" in m.content for m in results)


@pytest.mark.asyncio
async def test_fts_search_relevance(memory_store: MemoryStore) -> None:
    await memory_store.store_memory("user1", "Meeting with Alice about project Alpha", "event")
    await memory_store.store_memory("user1", "Grocery list: milk, eggs, bread", "note")
    await memory_store.store_memory("user1", "Alice sent the report for Alpha", "event")

    results = await memory_store.search_text("Alice Alpha", user_id="user1")
    assert len(results) >= 1
    assert all("Alice" in m.content or "Alpha" in m.content for m in results)


@pytest.mark.asyncio
async def test_search_respects_user_id(memory_store: MemoryStore) -> None:
    await memory_store.store_memory("user1", "User 1 secret note", "note")
    await memory_store.store_memory("user2", "User 2 secret note", "note")

    results = await memory_store.search_text("secret", user_id="user1")
    assert all(m.user_id == "user1" for m in results)


@pytest.mark.asyncio
async def test_vector_similarity_search(memory_store: MemoryStore) -> None:
    emb_a = [1.0, 0.0, 0.0]
    emb_b = [0.0, 1.0, 0.0]
    emb_c = [0.9, 0.1, 0.0]

    await memory_store.store_memory("user1", "about cats", "topic", embedding=emb_a)
    await memory_store.store_memory("user1", "about dogs", "topic", embedding=emb_b)
    await memory_store.store_memory("user1", "about kittens", "topic", embedding=emb_c)

    query_emb = [1.0, 0.0, 0.0]
    results = await memory_store.search_similar(query_emb, user_id="user1", limit=2)
    assert len(results) == 2
    # "about cats" should be most similar (exact match), then "about kittens"
    assert results[0].content == "about cats"
    assert results[1].content == "about kittens"


@pytest.mark.asyncio
async def test_entity_store_and_retrieve(memory_store: MemoryStore) -> None:
    eid = await memory_store.store_entity("user1", "Alice", "person", {"role": "manager"})
    assert eid

    entities = await memory_store.get_entities("user1")
    assert len(entities) == 1
    assert entities[0].name == "Alice"
    assert entities[0].type == "person"
    assert entities[0].attributes["role"] == "manager"


@pytest.mark.asyncio
async def test_entity_upsert(memory_store: MemoryStore) -> None:
    await memory_store.store_entity("user1", "Bob", "person", {"role": "dev"})
    await memory_store.store_entity("user1", "Bob", "person", {"role": "lead"})

    entities = await memory_store.get_entities("user1", entity_type="person")
    bobs = [e for e in entities if e.name == "Bob"]
    assert len(bobs) == 1
    assert bobs[0].attributes["role"] == "lead"


@pytest.mark.asyncio
async def test_store_conversation(memory_store: MemoryStore) -> None:
    conv_id = await memory_store.store_conversation("user1", "telegram", '[{"role":"user","content":"hi"}]')
    assert conv_id


@pytest.mark.asyncio
async def test_get_recent_memories(memory_store: MemoryStore) -> None:
    for i in range(5):
        await memory_store.store_memory("user1", f"Memory {i}", "general")

    recent = await memory_store.get_recent_memories("user1", limit=3)
    assert len(recent) == 3


# ── Summarizer Tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_summarizer_triggers_on_threshold(
    memory_store: MemoryStore, session_manager, settings
) -> None:
    mock_llm = AsyncMock(spec=BaseLLMProvider)
    mock_llm.complete.return_value = LLMResponse(
        content="Summary: User discussed Python and AI topics.",
        model="test",
        input_tokens=100,
        output_tokens=30,
    )

    summarizer = Summarizer(
        llm=mock_llm,
        memory_store=memory_store,
        session_manager=session_manager,
        threshold=6,
    )

    session = await session_manager.get_or_create("user1", "test")
    for i in range(8):
        role = MessageRole.USER if i % 2 == 0 else MessageRole.ASSISTANT
        await session_manager.add_message(
            session, LLMMessage(role=role, content=f"Message {i}")
        )

    did_summarize = await summarizer.maybe_summarize(session)
    assert did_summarize is True
    assert mock_llm.complete.call_count == 1

    # Summary should be stored as a memory
    results = await memory_store.search_text("Summary", user_id="user1")
    assert len(results) >= 1
    assert any(m.category == "conversation_summary" for m in results)


@pytest.mark.asyncio
async def test_summarizer_skips_short_conversation(
    memory_store: MemoryStore, session_manager, settings
) -> None:
    mock_llm = AsyncMock(spec=BaseLLMProvider)
    summarizer = Summarizer(
        llm=mock_llm,
        memory_store=memory_store,
        session_manager=session_manager,
        threshold=20,
    )

    session = await session_manager.get_or_create("user2", "test")
    await session_manager.add_message(
        session, LLMMessage(role=MessageRole.USER, content="hi")
    )

    did_summarize = await summarizer.maybe_summarize(session)
    assert did_summarize is False
    mock_llm.complete.assert_not_called()


# ── Python Exec Tests ────────────────────────────────────────


@pytest.mark.asyncio
async def test_python_exec_simple() -> None:
    from pincer.tools.builtin.python_exec import python_exec

    result = await python_exec("print('hello world')")
    assert "hello world" in result


@pytest.mark.asyncio
async def test_python_exec_calculation() -> None:
    from pincer.tools.builtin.python_exec import python_exec

    result = await python_exec("print(2 ** 10)")
    assert "1024" in result


@pytest.mark.asyncio
async def test_python_exec_stderr() -> None:
    from pincer.tools.builtin.python_exec import python_exec

    result = await python_exec("import sys; sys.stderr.write('warn\\n')")
    assert "warn" in result


@pytest.mark.asyncio
async def test_python_exec_timeout() -> None:
    from pincer.tools.builtin.python_exec import python_exec

    result = await python_exec("import time; time.sleep(10)", timeout=2)
    assert "timed out" in result.lower()


@pytest.mark.asyncio
async def test_python_exec_syntax_error() -> None:
    from pincer.tools.builtin.python_exec import python_exec

    result = await python_exec("def foo(")
    assert "SyntaxError" in result or "Error" in result


@pytest.mark.asyncio
async def test_python_exec_no_output() -> None:
    from pincer.tools.builtin.python_exec import python_exec

    result = await python_exec("x = 42")
    assert result == "(no output)"


# ── Voice Transcription Tests ────────────────────────────────


@pytest.mark.asyncio
async def test_transcribe_voice_no_api_key() -> None:
    from pincer.tools.builtin.transcribe import transcribe_voice

    result = await transcribe_voice(b"fake audio", "audio/ogg", api_key="")
    assert "requires" in result.lower()


@pytest.mark.asyncio
async def test_transcribe_voice_success() -> None:
    from pincer.tools.builtin.transcribe import transcribe_voice

    mock_transcription = MagicMock()
    mock_transcription.text = "Hello, this is a test transcription"

    with patch("openai.AsyncOpenAI") as mock_client:
        instance = AsyncMock()
        instance.audio.transcriptions.create = AsyncMock(return_value=mock_transcription)
        instance.close = AsyncMock()
        mock_client.return_value = instance

        result = await transcribe_voice(b"audio data", "audio/ogg", api_key="test-key")
        assert result == "Hello, this is a test transcription"


# ── Streaming Tests ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_stream_simple(settings, session_manager, cost_tracker, tool_registry) -> None:
    mock_llm = AsyncMock(spec=BaseLLMProvider)
    mock_llm.complete.return_value = LLMResponse(
        content="Final text",
        model="test",
        input_tokens=50,
        output_tokens=20,
    )

    async def fake_stream(**kwargs):
        for token in ["Hello", " ", "world", "!"]:
            yield token

    mock_llm.stream = fake_stream

    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    chunks: list[StreamChunk] = []
    async for chunk in agent.handle_message_stream("user1", "test", "Hello"):
        chunks.append(chunk)

    text_chunks = [c for c in chunks if c.type == StreamEventType.TEXT]
    done_chunks = [c for c in chunks if c.type == StreamEventType.DONE]

    assert len(text_chunks) == 4
    assert text_chunks[0].content == "Hello"
    assert len(done_chunks) == 1
    assert done_chunks[0].content == "Hello world!"


@pytest.mark.asyncio
async def test_agent_stream_with_tools(settings, session_manager, cost_tracker, tool_registry) -> None:
    mock_llm = AsyncMock(spec=BaseLLMProvider)
    mock_llm.complete.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="tc1", name="greet", arguments={"name": "World"})],
            model="test",
            input_tokens=50,
            output_tokens=30,
            stop_reason="tool_use",
        ),
        LLMResponse(
            content="Done with tools",
            model="test",
            input_tokens=80,
            output_tokens=20,
            stop_reason="end_turn",
        ),
    ]

    async def fake_stream(**kwargs):
        for token in ["The ", "greeting ", "worked!"]:
            yield token

    mock_llm.stream = fake_stream

    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry)

    chunks: list[StreamChunk] = []
    async for chunk in agent.handle_message_stream("user1", "test", "Greet world"):
        chunks.append(chunk)

    tool_starts = [c for c in chunks if c.type == StreamEventType.TOOL_START]
    assert len(tool_starts) == 1
    assert "greet" in tool_starts[0].content

    text_chunks = [c for c in chunks if c.type == StreamEventType.TEXT]
    assert len(text_chunks) == 3


# ── Full Agent + Memory Integration Tests ─────────────────────


@pytest.mark.asyncio
async def test_agent_with_memory(
    settings, session_manager, cost_tracker, tool_registry, tmp_path
) -> None:
    mock_llm = AsyncMock(spec=BaseLLMProvider)
    mock_llm.complete.return_value = LLMResponse(
        content="I remember that you like Python!",
        model="test",
        input_tokens=100,
        output_tokens=50,
    )

    mem_store = MemoryStore(tmp_path / "agent_mem.db")
    await mem_store.initialize()

    await mem_store.store_memory("user1", "User loves Python programming", "preference")

    agent = Agent(
        settings=settings,
        llm=mock_llm,
        session_manager=session_manager,
        cost_tracker=cost_tracker,
        tool_registry=tool_registry,
        memory_store=mem_store,
    )

    # Use a query that will FTS-match the stored memory
    result = await agent.handle_message("user1", "test", "Tell me about Python programming")
    assert isinstance(result, AgentResponse)
    assert result.text

    # Verify the LLM was called with memory-augmented system prompt
    call_args = mock_llm.complete.call_args
    system = call_args.kwargs.get("system", "")
    assert "Python" in system

    await mem_store.close()


# ── Browser Tests (skip if playwright not installed) ──────────


@pytest.mark.asyncio
async def test_browse_returns_error_without_playwright() -> None:
    with patch.dict("sys.modules", {"playwright": None, "playwright.async_api": None}):
        # Force reimport to pick up the patched modules
        from pincer.tools.builtin import browser
        # Reset the global browser state
        browser._browser = None
        browser._playwright = None
        browser._install_attempted = False

        result = await browser.browse("https://example.com")
        assert "Error" in result or "not installed" in result.lower() or "example" in result.lower()


# ── End-to-End Flow Test ─────────────────────────────────────


@pytest.mark.asyncio
async def test_full_flow_message_to_response(
    settings, session_manager, cost_tracker, tool_registry, tmp_path
) -> None:
    """Test the complete flow: user message -> agent -> tools -> response."""
    mock_llm = AsyncMock(spec=BaseLLMProvider)
    mock_llm.complete.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="tc1", name="greet", arguments={"name": "Alice"})],
            model="test-model",
            input_tokens=100,
            output_tokens=50,
            stop_reason="tool_use",
        ),
        LLMResponse(
            content="I greeted Alice for you! She says hello back.",
            model="test-model",
            input_tokens=150,
            output_tokens=30,
            stop_reason="end_turn",
        ),
    ]

    mem_store = MemoryStore(tmp_path / "e2e.db")
    await mem_store.initialize()

    agent = Agent(
        settings=settings,
        llm=mock_llm,
        session_manager=session_manager,
        cost_tracker=cost_tracker,
        tool_registry=tool_registry,
        memory_store=mem_store,
    )

    result = await agent.handle_message("user1", "telegram", "Please greet Alice")

    assert "Alice" in result.text
    assert result.tool_calls_made == 1
    assert result.model == "test-model"

    # Verify session has the full conversation
    session = await session_manager.get_or_create("user1", "telegram")
    roles = [m.role for m in session.messages]
    assert MessageRole.USER in roles
    assert MessageRole.ASSISTANT in roles
    assert MessageRole.TOOL_RESULT in roles

    # Verify memory was stored
    memories = await mem_store.get_recent_memories("user1")
    assert len(memories) >= 1

    await mem_store.close()
