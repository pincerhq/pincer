"""The phone book."""

from __future__ import annotations

from typing import Any

from pincer.db.session import session_scope
from pincer.repositories.voice import ContactRepository
from pincer.services.base import DatabaseService


class ContactsService(DatabaseService):
    async def search(self, fragment: str, limit: int = 5) -> list[dict[str, Any]]:
        async with session_scope(self._url) as session:
            rows = await ContactRepository(session).search_by_name(fragment, limit)
        return [{"name": name, "phone_number": number, "category": category} for name, number, category in rows]
