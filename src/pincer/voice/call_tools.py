"""
Pincer-owned in-call tools (Sprint 11) — the R/W tools from the tier table
that no integration provides:

- ``send_owner_message`` (W): relay a short message to the *initiating* user's
  own channel while the call is running (never to the callee).
- ``memory_note`` (W): store a note in cross-channel memory, tagged with the
  call, so it survives transcript retention.
- ``contact_lookup`` (R): read the ``phone_contacts`` table (the Sprint 7
  phone-contacts skill data) by name.
- ``call_memory_search`` (R, outbound only — issue #252): a few short snippets
  from the memory of the user who placed the call, never anyone else's.

Owner-facing results are linked back to where the call came from (#252):
messages also land in the chat session that asked for the call, and notes
are stored under the owner's canonical id with the call's thread/session tags.

All of them resolve the current call through the Sprint 9 call-cost ContextVar
(``current_call_sid``), so they only work inside a live turn; outside a call
they return an honest "Error: …" string (tool contract — never raise).
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pincer.memory.base import BaseMemoryBackend
    from pincer.tools.registry import ToolRegistry

from sqlalchemy.exc import OperationalError

from pincer.db.engine import get_database_url

logger = logging.getLogger(__name__)

MAX_MESSAGE_LEN = 500
MAX_NOTE_LEN = 500
MAX_SEARCH_RESULTS = 3
MAX_SNIPPET_LEN = 200
MAX_QUERY_LEN = 200


def _call_sid() -> str:
    from pincer.observability.call_costs import current_call_sid

    return current_call_sid()


def _owner_id(call_sid: str) -> str:
    """The canonical id of the user who placed this call ("" when unknown).

    Resolved server-side from the call's tracking, never from the turn
    context: on an outbound call that context's user is the callee's number."""
    from pincer.voice import status_notify

    return status_notify.owner_id_for_call(call_sid)  # tracking, else the pre-dial owner


async def _call_link_tags(call_sid: str, settings: Any) -> list[str]:
    """``thread:<id>`` / ``session:<key>`` tags for a call's notes (best effort)."""
    from pincer.voice import status_notify

    tags: list[str] = []
    try:
        from pincer.voice.threads import get_thread_manager

        thread_id = await get_thread_manager(settings).thread_for_call(call_sid)
    except Exception:
        logger.debug("Thread lookup for call note failed [%s]", call_sid, exc_info=True)
        thread_id = ""
    if thread_id:
        tags.append(f"thread:{thread_id}")
    info = status_notify.owner_info_for_call(call_sid)
    if info is not None and info.session_key:
        tags.append(f"session:{info.session_key}")
    return tags


def make_send_owner_message(settings: Any) -> Any:
    async def send_owner_message(text: str, context: dict | None = None) -> str:
        """Relay a short message to the user who initiated this call, on their
        own channel (e.g. Telegram). Use during a call to report an important
        fact or ask a quick question; the message is NOT shown to the call partner.
        """
        from pincer.voice import status_notify

        call_sid = _call_sid()
        if not call_sid:
            return "Error: send_owner_message is only available during a live phone call."
        body = str(text or "").strip()
        if not body:
            return "Error: empty message."
        body = body[:MAX_MESSAGE_LEN]
        info = status_notify.owner_info_for_call(call_sid)
        user_id = (info.user_id if info else "") or str((context or {}).get("pincer_user_id") or "")
        channel = info.channel if info else ""
        if not user_id:
            return "Error: no initiating user is known for this call."
        # Issue #252: the chat that asked for the call sees the message too.
        # Both deliveries at once: the call turn waits on this tool. The chat
        # copy is quoted and labelled as relayed: its wording may come from the
        # person on the phone, so it must read as a report, not as an instruction.
        target = (info.target_name or info.target_number) if info else ""
        quoted = body.replace('"', "'")
        delivered, linked = await asyncio.gather(
            status_notify.send_user_message(user_id, channel, f"📞 {body}"),
            status_notify.append_to_session(
                call_sid, f'📞 Message relayed during the call{f" with {target}" if target else ""}: "{quoted}"'
            ),
        )
        if delivered:
            if linked == status_notify.APPEND_FAILED:
                return "Message delivered to the user, but it could not be added to the chat that asked for this call."
            return "Message delivered to the user."
        if linked == status_notify.APPEND_DONE:
            return "The message was added to the user's chat, but the channel push failed."
        if linked == status_notify.APPEND_PENDING:
            # Not delivered yet, and not lost: say exactly that. Not an error —
            # the gate would defer it, and the follow-up would send it twice.
            return (
                "The channel push failed; a copy is queued for the user's chat and appears there once that "
                "chat is free. The user does not have the message yet."
            )
        return "Error: the message could not be delivered to the user's channel."

    return send_owner_message


def make_memory_note(settings: Any, memory: BaseMemoryBackend | None) -> Any:
    async def memory_note(note: str, context: dict | None = None) -> str:
        """Store a short note from this call in long-term memory (e.g. a preference
        the call partner stated, or something to follow up on). Tagged with the call.
        """
        call_sid = _call_sid()
        if not call_sid:
            return "Error: memory_note is only available during a live phone call."
        if memory is None:
            return "Error: memory is not enabled."
        body = str(note or "").strip()[:MAX_NOTE_LEN]
        if not body:
            return "Error: empty note."

        # The owner's Pincer identity: the key their chat recall searches
        # under. A call without one keeps the pre-#252 key (the routing user,
        # else the turn's user) — no canonical-id note, and call_memory_search
        # never reads it back.
        from pincer.voice import status_notify

        info = status_notify.owner_info_for_call(call_sid)
        user_id = (
            _owner_id(call_sid)
            or (info.user_id if info else "")
            or str((context or {}).get("pincer_user_id") or "")
            or "voice"
        )
        tags = ["source:voice_call", f"call:{call_sid}", "in_call_note", *await _call_link_tags(call_sid, settings)]
        try:
            await memory.store_memory(
                user_id=user_id,
                content=f"[Call note] {body}",
                category="voice_call",
                extra_tags=tags,
            )
        except Exception as e:
            logger.exception("memory_note failed [%s]", call_sid)
            return f"Error: could not store the note: {e}"
        return "Note stored."

    return memory_note


def make_memory_search(settings: Any, memory: BaseMemoryBackend | None) -> Any:
    async def call_memory_search(query: str) -> str:
        """Search the memory of the user who placed this call (outbound calls
        only). Returns at most a few short snippets."""
        call_sid = _call_sid()
        if not call_sid:
            return "Error: call_memory_search is only available during a live phone call."
        if memory is None:
            return "Error: memory is not enabled."
        text = str(query or "").strip()[:MAX_QUERY_LEN]
        if not text:
            return "Error: query is required."
        owner = _owner_id(call_sid)
        if not owner:
            # Never fall back to an unscoped search: that would read everyone's memory.
            return "Error: no initiating user is known for this call."
        try:
            found = await memory.search_text(text, user_id=owner, limit=MAX_SEARCH_RESULTS)
        except Exception as e:
            logger.exception("call_memory_search failed [%s]", call_sid)
            return f"Error: memory search failed: {e}"
        # Belt and braces: a backend that ignores user_id must not leak.
        # Whitespace collapsed: a multi-line memory must stay ONE snippet line.
        snippets = [
            " ".join(str(m.content or "").split())[:MAX_SNIPPET_LEN]
            for m in found
            if getattr(m, "user_id", owner) == owner and str(m.content or "").strip()
        ][:MAX_SEARCH_RESULTS]
        # One snippet per line: the shape the spoken renderer reads.
        return "\n".join(f"- {snippet}" for snippet in snippets) if snippets else "Nothing noted about that."

    return call_memory_search


def make_contact_lookup(settings: Any) -> Any:
    async def contact_lookup(name: str) -> str:
        """Look up a phone contact by (partial) name in the user's local phone contacts
        (not Google Contacts). Returns name, number and category; never returns anything
        else about the user.
        """
        from pincer.services.voice import ContactsService

        query = str(name or "").strip()
        if not query:
            return "Error: name is required."
        db_path = str(getattr(settings, "db_path", "") or "")
        if not db_path:
            return json.dumps([])
        try:
            contacts = ContactsService(get_database_url(Path(db_path)))
            rows = await contacts.search(query)
        except OperationalError:
            return json.dumps([])  # no database or no contacts table yet
        except Exception as e:
            logger.exception("contact_lookup failed")
            return f"Error: {e}"
        return json.dumps(
            [{"name": r["name"], "phone_number": r["phone_number"], "category": r["category"] or ""} for r in rows],
            ensure_ascii=False,
        )

    return contact_lookup


def register_call_tools(registry: ToolRegistry, settings: Any, memory: BaseMemoryBackend | None = None) -> list[str]:
    """Register the Pincer-owned in-call tools. Returns the registered names."""
    registry.register(
        name="send_owner_message",
        description=(
            "During a live phone call: relay a short message to the user who initiated the call, on their "
            "own channel (not to the call partner). Use for an important fact or a quick question."
        ),
        handler=make_send_owner_message(settings),
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Short message for the user"}},
            "required": ["text"],
        },
        require_approval=True,
    )
    registry.register(
        name="memory_note",
        description=(
            "During a live phone call: store a short note (a stated preference, a fact to remember, a "
            "follow-up) in long-term memory, tagged with this call."
        ),
        handler=make_memory_note(settings, memory),
        parameters={
            "type": "object",
            "properties": {"note": {"type": "string", "description": "The note to remember"}},
            "required": ["note"],
        },
        require_approval=True,
    )
    registry.register(
        name="contact_lookup",
        description=(
            "Look up a phone contact by (partial) name in the user's local phone-contacts list."
            " This does NOT search Google Contacts: an empty result only means the person is not in the"
            " phone-contacts list, not that they don't exist or aren't a saved Google contact."
        ),
        handler=make_contact_lookup(settings),
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Full or partial contact name"}},
            "required": ["name"],
        },
        require_approval=False,
    )
    registry.register(
        name="call_memory_search",
        description=(
            "During an outbound phone call: search the memory of the user you are calling for (preferences, "
            "addresses, earlier arrangements). Returns at most a few short snippets."
        ),
        handler=make_memory_search(settings, memory),
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What to look up"}},
            "required": ["query"],
        },
        require_approval=False,
    )
    return ["send_owner_message", "memory_note", "contact_lookup", "call_memory_search"]


__all__ = [
    "make_contact_lookup",
    "make_memory_note",
    "make_memory_search",
    "make_send_owner_message",
    "register_call_tools",
]
