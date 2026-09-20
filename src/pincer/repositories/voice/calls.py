"""The call log and what was said on it: `voice_calls`, `call_transcripts`, `call_actions`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import update
from sqlmodel import col, select

from pincer.db.dialect import dialect_of, upsert
from pincer.models.voice import CallAction, CallTranscript, VoiceCall
from pincer.repositories.base import BaseRepository

if TYPE_CHECKING:
    from collections.abc import Sequence


class CallRepository(BaseRepository[VoiceCall, int]):
    model = VoiceCall

    async def save(self, values: dict[str, Any]) -> None:
        """Insert the call row, or update the columns this caller knows about.

        Keyed on `call_sid`. Unlike the `INSERT OR REPLACE` this replaces, a
        column the caller does not pass keeps its stored value instead of
        being reset — which is what the thread columns needed patching back up
        after (see `pincer.services.voice.calls`).
        """
        stored = dict(values)
        await self.session.exec(
            upsert(
                dialect_of(self.session),
                VoiceCall,
                stored,
                index_elements=["call_sid"],
                set_=lambda excluded: {name: excluded[name] for name in stored if name != "call_sid"},
            )
        )

    async def by_sid(self, call_sid: str) -> VoiceCall | None:
        stmt = select(VoiceCall).where(col(VoiceCall.call_sid) == call_sid)
        return (await self.session.exec(stmt)).first()

    async def set_fields(self, call_sid: str, values: dict[str, Any]) -> int:
        stmt = update(VoiceCall).where(col(VoiceCall.call_sid) == call_sid).values(**values)
        return int((await self.session.exec(stmt)).rowcount)

    async def started_since(self, cutoff: str, *, direction: str | None = None) -> Sequence[VoiceCall]:
        where = [col(VoiceCall.started_at) >= cutoff]
        if direction:
            where.append(col(VoiceCall.direction) == direction)
        return await self.list(*where, order_by=[col(VoiceCall.started_at).desc()])


class TranscriptRepository(BaseRepository[CallTranscript, int]):
    model = CallTranscript

    async def add_many(self, rows: Sequence[CallTranscript]) -> None:
        self.session.add_all(list(rows))
        await self.session.flush()

    async def for_call(self, call_id: str) -> Sequence[CallTranscript]:
        return await self.list(col(CallTranscript.call_id) == call_id, order_by=[col(CallTranscript.timestamp)])


class CallActionRepository(BaseRepository[CallAction, int]):
    model = CallAction

    async def add_many(self, rows: Sequence[CallAction]) -> None:
        self.session.add_all(list(rows))
        await self.session.flush()

    async def for_call(self, call_id: str) -> Sequence[CallAction]:
        return await self.list(col(CallAction.call_id) == call_id, order_by=[col(CallAction.timestamp)])
