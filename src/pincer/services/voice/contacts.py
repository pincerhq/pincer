"""The phone book."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends

from pincer.db.session import session_scope
from pincer.repositories.voice import ContactRepository
from pincer.services.base import DatabaseService


class ContactsService(DatabaseService):
    async def all(self) -> list[dict[str, Any]]:
        async with session_scope(self._url) as session:
            rows = await ContactRepository(session).all_by_name()
        return [
            {"name": row.name, "phone_number": row.phone_number, "category": row.category, "notes": row.notes}
            for row in rows
        ]

    async def search(self, fragment: str, limit: int = 5) -> list[dict[str, Any]]:
        async with session_scope(self._url) as session:
            rows = await ContactRepository(session).search_by_name(fragment, limit)
        return [{"name": name, "phone_number": number, "category": category} for name, number, category in rows]


async def get_contacts_service() -> ContactsService:
    """FastAPI dependency: the phone book on the configured database."""
    from pincer.config import get_settings_relaxed

    return await ContactsService.for_path(get_settings_relaxed().db_path)


ContactsServiceDep = Annotated[ContactsService, Depends(get_contacts_service)]
