"""Messages the receptionist took."""

from __future__ import annotations

from typing import Any

from pincer.db.session import session_scope
from pincer.repositories.voice import CallRepository, InboundMessageRepository
from pincer.services.base import DatabaseService


class MessagesService(DatabaseService):
    async def record(self, values: dict[str, Any], *, inbound_intent: str = "") -> int:
        """Store the message and, when known, stamp the call's intent.

        One transaction: a message without its call's intent would leave the
        dashboard showing a taken message the call knows nothing about.
        """
        async with session_scope(self._url) as session:
            message_id = await InboundMessageRepository(session).replace_for_call(str(values["call_sid"]), values)
            if inbound_intent:
                await CallRepository(session).set_fields(str(values["call_sid"]), {"inbound_intent": inbound_intent})
        return message_id

    async def newest(self, limit: int) -> list[dict[str, Any]]:
        async with session_scope(self._url) as session:
            rows = await InboundMessageRepository(session).newest(limit)
        return [{name: getattr(row, name) for name in row.__class__.model_fields} for row in rows]

    async def mark_delivered(self, call_sid: str, when: str) -> None:
        async with session_scope(self._url) as session:
            await InboundMessageRepository(session).mark_delivered(call_sid, when)
