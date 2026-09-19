"""SQLModel table models — one module per domain, `table=True` only.

API request/response schemas are not defined here: they stay plain pydantic
models next to their routes. Every model module must be imported by this
package so `pincer.db.metadata` (and through it Alembic) sees every table.
"""
