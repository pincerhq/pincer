"""Messages the receptionist took: `inbound_messages`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import update
from sqlmodel import col

from pincer.models.voice import InboundMessage
from pincer.repositories.base import BaseRepository

if TYPE_CHECKING:
    from collections.abc import Sequence


class InboundMessageRepository(BaseRepository[InboundMessage, int]):
    model = InboundMessage

    async def replace_for_call(self, call_sid: str, values: dict[str, Any]) -> int:
        """One message per call: a re-take replaces the earlier one."""
        await self.delete_where(col(InboundMessage.call_sid) == call_sid)
        row = await self.add(InboundMessage(**values))
        return int(row.id or 0)

    async def mark_delivered(self, call_sid: str, when: str) -> int:
        stmt = update(InboundMessage).where(col(InboundMessage.call_sid) == call_sid).values(delivered_to_owner_at=when)
        return int((await self.session.exec(stmt)).rowcount)

    async def newest(self, limit: int) -> Sequence[InboundMessage]:
        return await self.list(
            order_by=[col(InboundMessage.created_at).desc(), col(InboundMessage.id).desc()], limit=limit
        )

    async def for_call(self, call_sid: str) -> Sequence[InboundMessage]:
        return await self.list(col(InboundMessage.call_sid) == call_sid)
