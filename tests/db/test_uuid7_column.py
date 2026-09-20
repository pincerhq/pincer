"""The `Uuid7` column type, on SQLite and Postgres.

Runs once per dialect (`db_url`); Postgres only when `PINCER_TEST_PG_URL` is
set. The model here lives on a private registry so it never reaches
`SQLModel.metadata`, which Alembic diffs against the real schema.

Postgres stores these as its native `uuid` and SQLite as the canonical string.
That difference is the accepted degradation, and everything below exists to
make sure it stays invisible from Python.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import registry
from sqlmodel import Field, SQLModel

from pincer.db.engine import get_engine
from pincer.db.ids import new_id
from pincer.db.session import session_scope
from pincer.db.types import Uuid7

_private = registry()


class _PrivateModel(SQLModel, registry=_private):
    """Passing `registry=` makes a class an abstract base; tables inherit it."""


class Keyed(_PrivateModel, table=True):
    __tablename__ = "phase10_keyed"

    id: str = Field(default=None, sa_column=sa.Column(Uuid7(), primary_key=True, default=new_id))
    label: str = Field(sa_column=sa.Column(sa.Text(), nullable=False))
    parent_id: str | None = Field(default=None, sa_column=sa.Column(Uuid7()))


@pytest.fixture
async def url(db_url: str):
    engine = get_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Keyed.metadata.drop_all)
        await conn.run_sync(Keyed.metadata.create_all)
    yield db_url
    async with engine.begin() as conn:
        await conn.run_sync(Keyed.metadata.drop_all)


async def _label_of(url: str, row_id: str) -> str | None:
    async with session_scope(url) as session:
        found = await session.get(Keyed, row_id)
        return None if found is None else found.label


# ── round trip ───────────────────────────────────────────────────────


async def test_an_id_round_trips_as_the_canonical_string(url):
    row_id = new_id()
    async with session_scope(url) as session:
        session.add(Keyed(id=row_id, label="first"))

    async with session_scope(url) as session:
        found = await session.get(Keyed, row_id)
    assert found is not None
    assert found.id == row_id  # a str, not a UUID, on both dialects
    assert isinstance(found.id, str)


async def test_a_column_default_supplies_an_id_without_the_caller(url):
    """The 14 converted tables rely on this: no sequence, no lastrowid."""
    async with session_scope(url) as session:
        row = Keyed(label="minted")
        session.add(row)
        await session.flush()
        minted = row.id

    assert uuid.UUID(minted).version == 7
    assert await _label_of(url, minted) == "minted"


async def test_any_spelling_of_a_uuid_is_stored_canonically(url):
    """So two rows cannot hold the same id in two different spellings."""
    canonical = new_id()
    for spelling in (canonical.upper(), f"{{{canonical}}}", canonical.replace("-", "")):
        async with session_scope(url) as session:
            session.add(Keyed(id=spelling, label=spelling))

        async with session_scope(url) as session:
            stored = (await session.exec(sa.select(Keyed.id).where(Keyed.label == spelling))).one()  # type: ignore[call-overload]
        assert stored[0] == canonical

        async with session_scope(url) as session:
            await session.exec(sa.delete(Keyed))  # type: ignore[call-overload]


async def test_the_empty_sentinel_is_stored_as_null(url):
    """`''` is the app's "no thread / no turn"; it is not a uuid, and on
    Postgres it would not cast. The column means NULL, so it stores NULL."""
    row_id = new_id()
    async with session_scope(url) as session:
        session.add(Keyed(id=row_id, label="orphan", parent_id=""))

    async with session_scope(url) as session:
        found = await session.get(Keyed, row_id)
    assert found is not None
    assert found.parent_id is None


async def test_a_value_that_is_not_a_uuid_is_refused_on_both_dialects(url):
    """Strict on SQLite too. A lenient branch there would let half the suite
    pass locally and fail only in CI's postgres job."""
    with pytest.raises(sa.exc.StatementError, match="badly formed"):
        async with session_scope(url) as session:
            session.add(Keyed(id="not-a-uuid", label="bad"))
            await session.flush()


# ── storage and ordering ─────────────────────────────────────────────


async def test_the_column_is_native_on_postgres_and_text_on_sqlite(url):
    engine = get_engine(url)

    def _column_type(sync_conn) -> str:
        columns = sa.inspect(sync_conn).get_columns("phase10_keyed")
        return str(next(c["type"] for c in columns if c["name"] == "id"))

    async with engine.connect() as conn:
        rendered = await conn.run_sync(_column_type)

    expected = "UUID" if engine.dialect.name == "postgresql" else "TEXT"
    assert rendered == expected


async def test_ids_order_by_time_in_the_database_not_just_in_python(url):
    """The reads that break a timestamp tie with the id depend on this."""
    ids = [new_id() for _ in range(25)]
    async with session_scope(url) as session:
        for index, row_id in enumerate(ids):
            session.add(Keyed(id=row_id, label=str(index)))

    async with session_scope(url) as session:
        ordered = (await session.exec(sa.select(Keyed.label).order_by(Keyed.id))).all()  # type: ignore[call-overload]
    assert [row[0] for row in ordered] == [str(index) for index in range(25)]
