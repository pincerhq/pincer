"""SessionManager and the session key format."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pincer.core.session import conversation_key
from pincer.llm.base import LLMMessage, MessageRole

if TYPE_CHECKING:
    from pincer.core.session import SessionManager

CONVERSATION = "22222222-2222-4222-8222-222222222222"


def test_conversation_key_nests_under_the_channel_key():
    assert conversation_key("alice", "web", "2222aaaa-2222-4222-8222-222222222222") == (
        "web:alice:2222aaaa-2222-4222-8222-222222222222"
    )


async def test_main_session_starts_fresh_when_only_conversations_exist(session_manager: SessionManager):
    """A user whose only web traffic was a conversation gets an empty main
    session, not that conversation's history."""
    key = conversation_key("u", "web", CONVERSATION)
    conversation = await session_manager.get_or_create("u", "web", session_key=key)
    await session_manager.add_message(conversation, LLMMessage(role=MessageRole.USER, content="conversation only"))
    await session_manager.evict(key)

    main = await session_manager.get_or_create("u", "web")

    assert main.session_id == "web:u"
    assert [m.content for m in main.messages] == []
