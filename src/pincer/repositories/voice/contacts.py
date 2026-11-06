"""The phone book: `phone_contacts`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func
from sqlmodel import col, select

from pincer.models.voice import PhoneContact
from pincer.repositories.base import BaseRepository

if TYPE_CHECKING:
    from collections.abc import Sequence


class ContactRepository(BaseRepository[PhoneContact, str]):
    model = PhoneContact

    async def all_by_name(self) -> Sequence[PhoneContact]:
        """Every contact, ordered by name regardless of case."""
        stmt = select(PhoneContact).order_by(func.lower(col(PhoneContact.name)))
        return (await self.session.exec(stmt)).all()

    async def search_by_name(self, fragment: str, limit: int = 5) -> Sequence[tuple[str, str, str | None]]:
        """Case-insensitive name search: (name, number, category).

        The fragment is split into words (on spaces and commas) and every word
        must occur in the name, so "Jane Doe" also finds "Doe, Jane".
        `lower()` on both sides rather than SQLite's `COLLATE NOCASE`, which
        Postgres does not have; same result for the names this matches. Only
        the three columns the caller shows are read — a contact's notes are
        not a phone number, and a hand-made table without the other columns
        still works.
        """
        tokens = fragment.replace(",", " ").lower().split() or [""]
        lowered = func.lower(col(PhoneContact.name))
        stmt = (
            select(PhoneContact.name, PhoneContact.phone_number, PhoneContact.category)
            .where(*(lowered.like(f"%{token}%") for token in tokens))
            .order_by(func.lower(col(PhoneContact.name)))
            .limit(limit)
        )
        return [(name, number, category) for name, number, category in (await self.session.exec(stmt)).all()]
