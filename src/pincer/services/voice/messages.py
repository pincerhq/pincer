"""Messages the receptionist took."""

from __future__ import annotations

import logging
from typing import Any

from pincer.db.session import session_scope
from pincer.repositories.voice import CallRepository, InboundMessageRepository
from pincer.services.base import DatabaseService

logger = logging.getLogger(__name__)


class MessagesService(DatabaseService):
    async def record(self, values: dict[str, Any], *, inbound_intent: str = "") -> int:
        """Store the message and, when known, stamp the call's intent.

        The intent is stamped separately and best-effort: it is a label on the
        call, and losing the message the caller left because the label could
        not be written would be the worse outcome.
        """
        call_sid = str(values["call_sid"])
        async with session_scope(self._url) as session:
            message_id = await InboundMessageRepository(session).replace_for_call(call_sid, values)

        if inbound_intent:
            try:
                async with session_scope(self._url) as session:
                    await CallRepository(session).set_fields(call_sid, {"inbound_intent": inbound_intent})
            except Exception:
                logger.debug("inbound_intent stamp failed [%s]", call_sid, exc_info=True)
        return message_id

    async def newest(self, limit: int) -> list[dict[str, Any]]:
        async with session_scope(self._url) as session:
            rows = await InboundMessageRepository(session).newest(limit)
        return [{name: getattr(row, name) for name in row.__class__.model_fields} for row in rows]

    async def mark_delivered(self, call_sid: str, when: str) -> None:
        async with session_scope(self._url) as session:
            await InboundMessageRepository(session).mark_delivered(call_sid, when)
