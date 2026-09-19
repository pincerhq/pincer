"""Unit of work, the base repository, the dialect helpers and the column types.

Runs once per dialect (`db_url`); Postgres only when `PINCER_TEST_PG_URL` is set.
The table models here live on a private registry so they never reach
`SQLModel.metadata`, which Alembic diffs against the real schema.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Column, Float, Integer, String, text
from sqlalchemy.orm import registry
from sqlmodel import Field, SQLModel, select

from pincer.db.dialect import day_bucket, dialect_of, json_contains, upsert
from pincer.db.engine import get_engine
from pincer.db.session import DbSession, session_scope
from pincer.db.types import IsoText, JSONText
from pincer.repositories.base import BaseRepository

_private = registry()


class _PrivateModel(SQLModel, registry=_private):
    """Passing `registry=` makes a class an abstract base; tables inherit it."""


class Widget(_PrivateModel, table=True):
    __tablename__ = "phase0_widgets"
    __table_args__ = {"sqlite_autoincrement": True}

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(sa_column=Column(String, unique=True, nullable=False))
    hits: int = Field(default=0, sa_column=Column(Integer, nullable=False, server_default="0"))
    status: str = Field(default="active", sa_column=Column(String, nullable=False, server_default="active"))
    seen_at: float = Field(default=0.0, sa_column=Column(Float, nullable=False, server_default="0"))
    stamped_at: str | None = Field(default=None, sa_column=Column(IsoText))
    tags: Any = Field(default=None, sa_column=Column(JSONText))


class WidgetRepository(BaseRepository[Widget, int]):
    model = Widget


@pytest.fixture
async def url(db_url: str):
    engine = get_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(_private.metadata.drop_all)
        await conn.run_sync(_private.metadata.create_all)
    yield db_url
    async with engine.begin() as conn:
        await conn.run_sync(_private.metadata.drop_all)


async def _names(url: str) -> list[str]:
    async with session_scope(url) as session:
        return sorted(w.name for w in await WidgetRepository(session).list())


# ── unit of work ─────────────────────────────────────────────────────


async def test_a_scope_commits_on_success(url):
    async with session_scope(url) as session:
        await WidgetRepository(session).add(Widget(name="a"))
    assert await _names(url) == ["a"]


async def test_a_scope_rolls_everything_back_when_it_raises(url):
    with pytest.raises(RuntimeError):
        async with session_scope(url) as session:
            repo = WidgetRepository(session)
            await repo.add(Widget(name="a"))
            await repo.add(Widget(name="b"))
            raise RuntimeError("abort the unit of work")
    assert await _names(url) == []


def test_a_route_gets_one_unit_of_work_per_request(url, monkeypatch):
    monkeypatch.setattr("pincer.db.session.get_engine", lambda _url=None: get_engine(url))
    app = FastAPI()

    @app.post("/widgets/{name}")
    async def create(name: str, session: DbSession) -> dict[str, Any]:
        widget = await WidgetRepository(session).add(Widget(name=name))
        return {"id": widget.id, "dialect": dialect_of(session)}

    with TestClient(app) as client:
        body = client.post("/widgets/a").json()
    assert body["id"] == 1
    assert body["dialect"] in ("sqlite", "postgresql")


# ── base repository ──────────────────────────────────────────────────


async def test_add_returns_the_generated_id_and_server_defaults(url):
    async with session_scope(url) as session:
        widget = await WidgetRepository(session).add(Widget(name="a"))
        assert widget.id == 1
        assert widget.status == "active"


async def test_get_list_and_deletes(url):
    async with session_scope(url) as session:
        repo = WidgetRepository(session)
        for i, name in enumerate(["a", "b", "c"]):
            await repo.add(Widget(name=name, seen_at=float(i)))

    async with session_scope(url) as session:
        repo = WidgetRepository(session)
        assert (await repo.get(2)).name == "b"
        assert await repo.get(99) is None
        newest = await repo.list(Widget.seen_at >= 1, order_by=[Widget.seen_at.desc()], limit=1)
        assert [w.name for w in newest] == ["c"]
        assert await repo.delete_older_than(Widget.seen_at, 1.0) == 1
        assert await repo.delete_where(Widget.name == "b") == 1
    assert await _names(url) == ["c"]


# ── dialect helpers ──────────────────────────────────────────────────


async def test_upsert_without_assignments_ignores_the_conflict(url):
    async with session_scope(url) as session:
        dialect = dialect_of(session)
        for hits in (1, 2):
            await session.exec(upsert(dialect, Widget, {"name": "a", "hits": hits}, index_elements=["name"]))
    async with session_scope(url) as session:
        assert (await session.exec(select(Widget.hits))).one() == 1


async def test_upsert_can_accumulate_from_the_excluded_row(url):
    table = Widget.__table__
    async with session_scope(url) as session:
        dialect = dialect_of(session)
        for hits in (1, 2, 4):
            await session.exec(
                upsert(
                    dialect,
                    Widget,
                    {"name": "a", "hits": hits},
                    index_elements=["name"],
                    set_=lambda excluded: {"hits": table.c.hits + excluded.hits},
                )
            )
    async with session_scope(url) as session:
        assert (await session.exec(select(Widget.hits))).one() == 7


async def test_upsert_where_guards_the_update(url):
    """The shape telemetry's absorbing status needs: a closed row stays closed."""
    table = Widget.__table__
    async with session_scope(url) as session:
        dialect = dialect_of(session)
        for status in ("ended", "active"):
            await session.exec(
                upsert(
                    dialect,
                    Widget,
                    {"name": "a", "status": status},
                    index_elements=["name"],
                    set_=lambda excluded: {"status": excluded.status},
                    where=table.c.status == "active",
                )
            )
    async with session_scope(url) as session:
        # The first write inserted "ended"; the second hit the guard and was skipped.
        assert (await session.exec(select(Widget.status))).one() == "ended"


async def test_json_contains_matches_array_elements(url):
    async with session_scope(url) as session:
        repo = WidgetRepository(session)
        await repo.add(Widget(name="a", tags=["work", "urgent"]))
        await repo.add(Widget(name="b", tags=["home"]))
        await repo.add(Widget(name="c", tags=[]))

    async with session_scope(url) as session:
        dialect = dialect_of(session)
        found = await WidgetRepository(session).list(json_contains(dialect, Widget.__table__.c.tags, "urgent"))
    assert [w.name for w in found] == ["a"]


async def test_day_bucket_is_the_utc_calendar_day(url):
    async with session_scope(url) as session:
        repo = WidgetRepository(session)
        await repo.add(Widget(name="a", seen_at=1_767_225_599.0))  # 2025-12-31T23:59:59Z
        await repo.add(Widget(name="b", seen_at=1_767_225_600.0))  # 2026-01-01T00:00:00Z

    async with session_scope(url) as session:
        bucket = day_bucket(dialect_of(session), Widget.__table__.c.seen_at)
        rows = (await session.exec(select(bucket).order_by(Widget.name))).all()
    assert rows == ["2025-12-31", "2026-01-01"]


# ── column types ─────────────────────────────────────────────────────


async def test_column_types_round_trip_python_values(url):
    async with session_scope(url) as session:
        await WidgetRepository(session).add(
            Widget(name="a", stamped_at="2026-09-19T10:00:00+00:00", tags={"k": [1, 2]})
        )
    async with session_scope(url) as session:
        widget = (await WidgetRepository(session).list())[0]
    assert widget.stamped_at == "2026-09-19T10:00:00+00:00"
    assert widget.tags == {"k": [1, 2]}


async def test_iso_text_keeps_the_instant_across_offsets(url):
    """A non-UTC offset is the same instant; Postgres hands it back in UTC."""
    async with session_scope(url) as session:
        await WidgetRepository(session).add(Widget(name="a", stamped_at="2026-09-19T12:00:00+02:00"))
    async with session_scope(url) as session:
        stamped = (await WidgetRepository(session).list())[0].stamped_at
    assert datetime.fromisoformat(stamped) == datetime(2026, 9, 19, 10, 0, tzinfo=UTC)


async def test_json_text_is_stored_as_text_and_reads_empty_as_none(url):
    async with session_scope(url) as session:
        # Raw, so the empty string reaches the column untouched by JSONText.
        conn = await session.connection()
        await conn.execute(text("INSERT INTO phase0_widgets (name, tags) VALUES ('a', '')"))
    async with session_scope(url) as session:
        assert (await WidgetRepository(session).list())[0].tags is None
