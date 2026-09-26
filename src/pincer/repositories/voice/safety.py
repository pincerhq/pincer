"""The dialling gate's records: `do_not_call_numbers`, `outbound_call_logs`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func
from sqlmodel import col, select

from pincer.db.dialect import dialect_of, upsert
from pincer.models.voice import DoNotCallNumber, OutboundCallLog
from pincer.repositories.base import BaseRepository

if TYPE_CHECKING:
    from collections.abc import Sequence


class DoNotCallRepository(BaseRepository[DoNotCallNumber, str]):
    model = DoNotCallNumber

    async def contains(self, phone_number: str) -> bool:
        stmt = select(DoNotCallNumber.phone_number).where(col(DoNotCallNumber.phone_number) == phone_number)
        return (await self.session.exec(stmt)).first() is not None

    async def add(self, values: dict[str, str]) -> None:  # type: ignore[override]
        """Record an objection. A repeat keeps the original `added_at`: the
        date the callee first objected is the one that matters."""
        await self.session.exec(
            upsert(
                dialect_of(self.session),
                DoNotCallNumber,
                values,
                index_elements=["phone_number"],
                set_=lambda excluded: {"reason": excluded.reason, "source": excluded.source},
            )
        )

    async def remove(self, phone_number: str) -> int:
        return await self.delete_where(col(DoNotCallNumber.phone_number) == phone_number)

    async def newest_first(self) -> Sequence[DoNotCallNumber]:
        return await self.list(order_by=[col(DoNotCallNumber.added_at).desc()])


class OutboundCallLogRepository(BaseRepository[OutboundCallLog, str]):
    model = OutboundCallLog

    async def count_for_day(self, local_day: str) -> int:
        stmt = select(func.count()).select_from(OutboundCallLog).where(col(OutboundCallLog.local_day) == local_day)
        return int((await self.session.exec(stmt)).one())

    async def dialled_after_objection(self, cutoff: str) -> Sequence[tuple[str, str]]:
        """Dials placed to a number that had already objected — the compliance
        violation the gate exists to prevent."""
        stmt = (
            select(OutboundCallLog.phone_number, OutboundCallLog.placed_at)
            .join(DoNotCallNumber, col(DoNotCallNumber.phone_number) == col(OutboundCallLog.phone_number))
            .where(
                col(OutboundCallLog.placed_at) >= cutoff, col(OutboundCallLog.placed_at) > col(DoNotCallNumber.added_at)
            )
        )
        return [(number, placed) for number, placed in (await self.session.exec(stmt)).all()]

    async def placed_since(self, phone_number: str, cutoff: str) -> Sequence[str]:
        stmt = (
            select(OutboundCallLog.placed_at)
            .where(col(OutboundCallLog.phone_number) == phone_number, col(OutboundCallLog.placed_at) >= cutoff)
            .order_by(col(OutboundCallLog.placed_at))
        )
        return (await self.session.exec(stmt)).all()
