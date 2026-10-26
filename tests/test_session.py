"""SessionManager and the session key format."""

from __future__ import annotations

from pincer.core.session import conversation_key


def test_conversation_key_nests_under_the_channel_key():
    assert conversation_key("alice", "web", "2222aaaa-2222-4222-8222-222222222222") == (
        "web:alice:2222aaaa-2222-4222-8222-222222222222"
    )
