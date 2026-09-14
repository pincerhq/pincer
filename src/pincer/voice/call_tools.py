"""
Pincer-owned in-call tools (Sprint 11) — the R/W tools from the tier table
that no integration provides:

- ``send_owner_message`` (W): relay a short message to the *initiating* user's
  own channel while the call is running (never to the callee).
- ``memory_note`` (W): store a note in cross-channel memory, tagged with the
  call, so it survives transcript retention.
- ``contact_lookup`` (R): read the ``phone_contacts`` table (the Sprint 7
  phone-contacts skill data) by name.

All three resolve the current call through the Sprint 9 call-cost ContextVar
(``current_call_sid``), so they only work inside a live turn; outside a call
they return an honest "Error: …" string (tool contract — never raise).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pincer.memory.base import BaseMemoryBackend
    from pincer.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

MAX_MESSAGE_LEN = 500
MAX_NOTE_LEN = 500


def _call_sid() -> str:
    from pincer.observability.call_costs import current_call_sid

    return current_call_sid()


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
        info = status_notify.get_call_info(call_sid)
        user_id = (info.user_id if info else "") or str((context or {}).get("pincer_user_id") or "")
        channel = info.channel if info else ""
        if not user_id:
            return "Error: no initiating user is known for this call."
        delivered = await status_notify.send_user_message(user_id, channel, f"📞 {body}")
        if not delivered:
            return "Error: the message could not be delivered to the user's channel."
        return "Message delivered to the user."

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
        from pincer.voice import status_notify

        info = status_notify.get_call_info(call_sid)
        user_id = (info.user_id if info else "") or str((context or {}).get("pincer_user_id") or "") or "voice"
        try:
            await memory.store_memory(
                user_id=user_id,
                content=f"[Call note] {body}",
                category="voice_call",
                extra_tags=["source:voice_call", f"call:{call_sid}", "in_call_note"],
            )
        except Exception as e:
            logger.exception("memory_note failed [%s]", call_sid)
            return f"Error: could not store the note: {e}"
        return "Note stored."

    return memory_note


def make_contact_lookup(settings: Any) -> Any:
    async def contact_lookup(name: str) -> str:
        """Look up a phone contact by (partial) name in the user's phone contacts.
        Returns name, number and category; never returns anything else about the user.
        """
        import aiosqlite

        query = str(name or "").strip()
        if not query:
            return "Error: name is required."
        db_path = str(getattr(settings, "db_path", "") or "")
        if not db_path:
            return json.dumps([])
        try:
            async with aiosqlite.connect(db_path) as db:
                db.row_factory = aiosqlite.Row
                rows = await db.execute_fetchall(
                    "SELECT name, phone_number, category FROM phone_contacts "
                    "WHERE name LIKE ? ORDER BY name COLLATE NOCASE ASC LIMIT 5",
                    (f"%{query}%",),
                )
        except aiosqlite.OperationalError:
            return json.dumps([])  # table not created yet — no contacts
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
        description="Look up a phone contact by (partial) name in the user's phone contacts.",
        handler=make_contact_lookup(settings),
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Full or partial contact name"}},
            "required": ["name"],
        },
        require_approval=False,
    )
    return ["send_owner_message", "memory_note", "contact_lookup"]


__all__ = [
    "make_contact_lookup",
    "make_memory_note",
    "make_send_owner_message",
    "register_call_tools",
]
