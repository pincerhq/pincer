"""SQLModel table models — one module per domain, `table=True` only.

API request/response schemas are not defined here: they stay plain pydantic
models next to their routes. Every model module must be imported by this
package so `pincer.db.metadata` (and through it Alembic) sees every table.
"""

from pincer.models import (
    audit,
    costs,
    identity,
    memory,
    observability,
    scheduler,
    sessions,
    telephony,
    voice,
)

__all__ = [
    "audit",
    "costs",
    "identity",
    "memory",
    "observability",
    "scheduler",
    "sessions",
    "telephony",
    "voice",
]
