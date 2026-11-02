"""#305: the user's preferred language — profile helpers, the
``set_preferred_language`` tool, and an inbound call starting in it."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from pincer.core.agent import Agent
from pincer.llm.base import LLMResponse, ToolCall
from pincer.memory.profile import (
    clean_language,
    get_preferred_language,
    profile_language,
    set_preferred_language,
    with_language,
)
from pincer.memory.sqlite import SQLiteMemoryBackend
from pincer.tools.builtin.language_pref import make_set_preferred_language, register_language_tools

if TYPE_CHECKING:
    from pathlib import Path


@pytest_asyncio.fixture
async def memory(tmp_path: Path) -> SQLiteMemoryBackend:
    backend = SQLiteMemoryBackend(tmp_path / "memory.db")
    await backend.initialize()
    yield backend  # type: ignore[misc]
    await backend.close()


# ── profile helpers ──────────────────────────────────────────────────


def test_profile_language_parsing():
    assert profile_language("The user's name is Olena. Preferred language: uk.") == "uk"
    assert profile_language("The user's name is Olena.") is None


def test_with_language_replaces_or_appends():
    assert with_language("The user's name is Olena. Preferred language: en.", "uk") == (
        "The user's name is Olena. Preferred language: uk."
    )
    assert with_language("The user's name is Olena.", "uk") == "The user's name is Olena. Preferred language: uk."
    assert with_language("", "uk") == "Preferred language: uk."


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("UK", "uk"), (" de ", "de"), ("Ukrainian", "Ukrainian"), ("", None), ("u.k", None), ("x" * 41, None)],
)
def test_clean_language(raw, expected):
    assert clean_language(raw) == expected


async def test_set_keeps_the_rest_of_the_profile(memory: SQLiteMemoryBackend):
    await memory.add_profile(user_id="u1", name="Olena", use_case="email", language="en")

    await set_preferred_language(memory, "u1", "uk")

    profiles = await memory.list_memories(user_id="u1", category="profile")
    assert len(profiles) == 1
    assert "Olena" in profiles[0].content and "email" in profiles[0].content
    assert await get_preferred_language(memory, "u1") == "uk"


async def test_set_without_a_profile_creates_one(memory: SQLiteMemoryBackend):
    assert await get_preferred_language(memory, "u1") is None

    await set_preferred_language(memory, "u1", "uk")
    await set_preferred_language(memory, "u1", "en")

    assert await get_preferred_language(memory, "u1") == "en"
    assert len(await memory.list_memories(user_id="u1", category="profile")) == 1
    assert await get_preferred_language(memory, "u2") is None


# ── the tool ─────────────────────────────────────────────────────────


async def test_tool_stores_the_language_for_the_canonical_user(memory: SQLiteMemoryBackend):
    tool = make_set_preferred_language(memory)

    result = await tool("uk", context={"user_id": "123456", "pincer_user_id": "u1"})

    assert "uk" in result and not result.startswith("Error")
    assert await get_preferred_language(memory, "u1") == "uk"


async def test_tool_rejects_bad_input(memory: SQLiteMemoryBackend):
    tool = make_set_preferred_language(memory)
    assert (await tool("uk", context={})).startswith("Error")
    assert (await tool("", context={"pincer_user_id": "u1"})).startswith("Error")
    assert await get_preferred_language(memory, "u1") is None


async def test_tool_reports_a_storage_failure():
    broken = AsyncMock()
    broken.list_memories.side_effect = ConnectionError("down")
    tool = make_set_preferred_language(broken)
    assert (await tool("uk", context={"pincer_user_id": "u1"})).startswith("Error")


async def test_switch_persists_into_the_next_conversation(
    settings, mock_llm, session_manager, cost_tracker, tool_registry, memory: SQLiteMemoryBackend
):
    """The user asks in English for Ukrainian: the model calls the tool, and a
    later conversation's prompt carries the language with no search hit."""
    register_language_tools(tool_registry, memory)
    agent = Agent(settings, mock_llm, session_manager, cost_tracker, tool_registry, memory_store=memory)
    mock_llm.complete.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="tc1", name="set_preferred_language", arguments={"language": "uk"})],
            model="test",
            input_tokens=50,
            output_tokens=10,
            stop_reason="tool_use",
        ),
        LLMResponse(content="Гаразд, відтепер українською.", model="test", input_tokens=50, output_tokens=10),
        LLMResponse(content="Ось прогноз.", model="test", input_tokens=50, output_tokens=10),
    ]

    first = await agent.handle_message("u1", "test", "Please speak Ukrainian from now on")
    assert first.tool_calls_made == 1
    first_system = mock_llm.complete.call_args_list[0].kwargs["system"]
    assert "call set_preferred_language" in first_system

    await agent.handle_message("u1", "other-session", "What's the weather tomorrow?")

    later_system = mock_llm.complete.call_args_list[-1].kwargs["system"]
    assert "[User preferred language: uk]" in later_system


# ── inbound calls start in the caller's language ─────────────────────


async def test_inbound_call_language_from_the_callers_profile(monkeypatch, memory: SQLiteMemoryBackend):
    from pincer.voice import twiml_server

    await set_preferred_language(memory, "u1", "uk")
    owners = {"+4915112345678": "u1"}

    class FakeIdentity:
        def __init__(self, url):
            pass

        async def user_for_confirmed_phone(self, phone):
            return owners.get(phone)

    monkeypatch.setattr("pincer.services.identity.IdentityService", FakeIdentity)
    monkeypatch.setattr(twiml_server, "_settings", SimpleNamespace(db_path="unused.db"))
    monkeypatch.setattr(twiml_server, "_memory", memory)

    assert await twiml_server._caller_preferred_language("+4915112345678") == "uk"
    assert await twiml_server._caller_preferred_language("+4915100000000") == ""  # a guest
    assert await twiml_server._caller_preferred_language("") == ""


async def test_inbound_call_language_lookup_failure_uses_the_default(monkeypatch):
    from pincer.voice import twiml_server

    class BrokenIdentity:
        def __init__(self, url):
            pass

        async def user_for_confirmed_phone(self, phone):
            raise RuntimeError("db down")

    monkeypatch.setattr("pincer.services.identity.IdentityService", BrokenIdentity)
    monkeypatch.setattr(twiml_server, "_settings", SimpleNamespace(db_path="unused.db"))
    monkeypatch.setattr(twiml_server, "_memory", AsyncMock())

    assert await twiml_server._caller_preferred_language("+4915112345678") == ""


async def test_no_memory_means_no_lookup(monkeypatch):
    from pincer.voice import twiml_server

    monkeypatch.setattr(twiml_server, "_memory", None)
    assert await twiml_server._caller_preferred_language("+4915112345678") == ""
