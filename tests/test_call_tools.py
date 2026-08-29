"""Sprint 11 — the Pincer-owned in-call tools (send_owner_message, memory_note, contact_lookup)."""

from __future__ import annotations

import json

import aiosqlite
import pytest

from pincer.observability.call_costs import call_context
from pincer.tools.registry import ToolRegistry
from pincer.voice import status_notify
from pincer.voice.call_tools import register_call_tools
from pincer.voice.tool_policy import TIERS


@pytest.fixture(autouse=True)
def _clean():
    status_notify._reset_for_tests()
    yield
    status_notify._reset_for_tests()


class _Mem:
    def __init__(self):
        self.stored = []

    async def store_memory(self, **kwargs):
        self.stored.append(kwargs)
        return "m1"


async def test_registered_names_are_in_the_tier_table(settings):
    registry = ToolRegistry()
    names = register_call_tools(registry, settings, memory=_Mem())
    assert set(names) <= set(TIERS)
    assert set(names) <= set(registry.list_tools())
    assert registry.requires_approval("send_owner_message")  # generic (text-chat) approval stays on
    assert not registry.requires_approval("contact_lookup")


async def test_send_owner_message_reaches_initiating_user(settings):
    delivered = []

    async def notifier(user_id, channel, text):
        delivered.append((user_id, channel, text))
        return True

    status_notify.set_status_notifier(notifier)
    status_notify.register_outbound_call("CA9", user_id="12345", channel="telegram")
    registry = ToolRegistry()
    register_call_tools(registry, settings)

    assert (await registry.execute("send_owner_message", {"text": "hi"})).startswith("Error")  # outside a call
    with call_context("CA9"):
        result = await registry.execute("send_owner_message", {"text": "She prefers Tuesday."})
    assert result == "Message delivered to the user."
    assert delivered == [("12345", "telegram", "📞 She prefers Tuesday.")]


async def test_memory_note_tags_the_call(settings):
    mem = _Mem()
    status_notify.register_outbound_call("CA9", user_id="12345", channel="telegram")
    registry = ToolRegistry()
    register_call_tools(registry, settings, memory=mem)
    with call_context("CA9"):
        assert await registry.execute("memory_note", {"note": "prefers mornings"}) == "Note stored."
    assert mem.stored[0]["user_id"] == "12345"
    assert "call:CA9" in mem.stored[0]["extra_tags"]
    assert mem.stored[0]["content"] == "[Call note] prefers mornings"
    # no memory backend → honest error, never a crash
    registry2 = ToolRegistry()
    register_call_tools(registry2, settings, memory=None)
    with call_context("CA9"):
        assert (await registry2.execute("memory_note", {"note": "x"})).startswith("Error")


async def test_contact_lookup_reads_phone_contacts(settings, tmp_path):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(settings.db_path) as db:
        await db.execute("CREATE TABLE phone_contacts (name TEXT, phone_number TEXT, category TEXT, notes TEXT)")
        await db.execute("INSERT INTO phone_contacts VALUES ('Dr. Müller', '+4930123', 'doctor', 'secret notes')")
        await db.commit()
    registry = ToolRegistry()
    register_call_tools(registry, settings)
    rows = json.loads(await registry.execute("contact_lookup", {"name": "müll"}))
    assert rows == [{"name": "Dr. Müller", "phone_number": "+4930123", "category": "doctor"}]
    assert json.loads(await registry.execute("contact_lookup", {"name": "nobody"})) == []


async def test_contact_lookup_without_table_is_empty(settings):
    registry = ToolRegistry()
    register_call_tools(registry, settings)
    assert json.loads(await registry.execute("contact_lookup", {"name": "x"})) == []
