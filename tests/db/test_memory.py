"""Memory on the repository/service layer, on SQLite and Postgres.

Full-text search needs the schema the migrations build (the FTS5 table and its
triggers on SQLite, the tsvector column on Postgres), so these run against a
migrated database rather than `create_all`.
"""

from __future__ import annotations

import json

import pytest

from pincer.db.engine import get_engine
from pincer.models.memory import Conversation, Entity, Memory
from pincer.services.memory import MemoryService

_TABLES = [Memory.__table__, Entity.__table__, Conversation.__table__]


@pytest.fixture
async def url(db_url: str):
    engine = get_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Memory.metadata.drop_all(c, tables=_TABLES))
        await conn.run_sync(lambda c: Memory.metadata.create_all(c, tables=_TABLES))
    yield db_url
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Memory.metadata.drop_all(c, tables=_TABLES))


async def _store(url: str, memory_id: str, *, user_id: str = "usr_a", content: str = "hello", **kwargs) -> None:
    values = {
        "id": memory_id,
        "user_id": user_id,
        "content": content,
        "category": kwargs.pop("category", "general"),
        "tags": json.dumps(kwargs.pop("tags", [])),
        "created_at": kwargs.pop("created_at", 1000.0),
        **kwargs,
    }
    await MemoryService(url).add(values)


# ── memories ─────────────────────────────────────────────────────────


async def test_a_memory_round_trips(url):
    service = MemoryService(url)
    await _store(url, "m1", content="the boiler is serviced in March", tags=["user:usr_a", "home"])

    stored = await service.get("m1")
    assert stored["content"] == "the boiler is serviced in March"
    assert json.loads(stored["tags"]) == ["user:usr_a", "home"]
    assert await service.get("missing") is None


async def test_listing_is_newest_first_and_pages(url):
    service = MemoryService(url)
    for i in range(5):
        await _store(url, f"m{i}", created_at=float(i))

    newest = await service.search(user_id="usr_a", limit=2)
    assert [row["id"] for row in newest] == ["m4", "m3"]
    assert [row["id"] for row in await service.search(user_id="usr_a", limit=2, offset=2)] == ["m2", "m1"]


async def test_tag_filters_match_any_or_all(url):
    service = MemoryService(url)
    await _store(url, "both", tags=["work", "urgent"])
    await _store(url, "one", tags=["work"])
    await _store(url, "none", tags=["home"])

    any_of = await service.search(tags=["work", "urgent"])
    assert {row["id"] for row in any_of} == {"both", "one"}

    all_of = await service.search(tags=["work", "urgent"], match_all_tags=True)
    assert {row["id"] for row in all_of} == {"both"}

    assert await service.count(tags=["work"]) == 2
    assert await service.count(tags=["work", "urgent"], match_all_tags=True) == 1


async def test_a_memory_with_no_tags_is_not_matched_by_a_tag_filter(url):
    """The tags column defaults to '[]' — parsing that must not error."""
    service = MemoryService(url)
    await _store(url, "untagged")
    assert await service.search(tags=["work"]) == []
    assert await service.count(tags=["work"]) == 0


async def test_updates_and_deletes(url):
    service = MemoryService(url)
    await _store(url, "m1", content="before", category="general")

    await service.set_fields("m1", {"content": "after", "category": "profile"})
    stored = await service.get("m1")
    assert (stored["content"], stored["category"]) == ("after", "profile")

    await service.set_fields("m1", {})  # nothing to change is not an error
    await service.delete("m1")
    assert await service.get("m1") is None


async def test_deleting_a_user_s_memories_can_be_scoped_to_a_category(url):
    service = MemoryService(url)
    await _store(url, "p1", category="profile")
    await _store(url, "g1", category="general")
    await _store(url, "other", user_id="usr_b", category="profile")

    assert await service.delete_for_user("usr_a", category="profile") == 1
    assert {row["id"] for row in await service.search()} == {"g1", "other"}
    assert await service.delete_for_user("usr_a") == 1
    assert {row["id"] for row in await service.search()} == {"other"}


async def test_stats_count_users_and_categories(url):
    service = MemoryService(url)
    await _store(url, "m1", user_id="usr_a", category="general")
    await _store(url, "m2", user_id="usr_a", category="profile")
    await _store(url, "m3", user_id="usr_b", category="general")

    users, by_category = await service.stats()
    assert users == 2
    assert by_category == {"general": 2, "profile": 1}


# ── entities ─────────────────────────────────────────────────────────


async def test_an_entity_is_one_row_per_user_name_and_type(url):
    service = MemoryService(url)
    await service.upsert_entity(
        {"id": "e1", "user_id": "usr_a", "name": "Ada", "type": "person", "attributes_json": "{}", "last_seen": 1.0}
    )
    await service.upsert_entity(
        {
            "id": "e2",
            "user_id": "usr_a",
            "name": "Ada",
            "type": "person",
            "attributes_json": '{"role": "dentist"}',
            "last_seen": 2.0,
        }
    )

    rows = await service.entities_for("usr_a", limit=10)
    assert len(rows) == 1
    assert rows[0]["id"] == "e1"  # the first sighting keeps its id
    assert json.loads(rows[0]["attributes_json"]) == {"role": "dentist"}
    assert rows[0]["last_seen"] == 2.0


async def test_entities_are_newest_first_and_filtered_by_type(url):
    service = MemoryService(url)
    for entity_id, type_, last_seen in (("e1", "person", 1.0), ("e2", "place", 2.0), ("e3", "person", 3.0)):
        await service.upsert_entity(
            {
                "id": entity_id,
                "user_id": "usr_a",
                "name": entity_id,
                "type": type_,
                "attributes_json": "{}",
                "last_seen": last_seen,
            }
        )

    assert [row["id"] for row in await service.entities_for("usr_a", limit=10)] == ["e3", "e2", "e1"]
    assert [row["id"] for row in await service.entities_for("usr_a", type_="person", limit=10)] == ["e3", "e1"]


# ── full-text search ─────────────────────────────────────────────────


async def test_full_text_search_finds_and_ranks(migrated_url):
    service = MemoryService(migrated_url)
    await _store(migrated_url, "boiler", content="the boiler is serviced every March")
    await _store(migrated_url, "dentist", content="dentist appointment in March")
    await _store(migrated_url, "unrelated", content="nothing to do with anything")

    found = await service.full_text("boiler")
    assert [row["id"] for row in found] == ["boiler"]
    assert found[0]["score"] > 0

    # Several words are ORed, as the old FTS5 query did.
    assert {row["id"] for row in await service.full_text("boiler dentist")} == {"boiler", "dentist"}
    assert await service.full_text("") == []
    assert await service.full_text("nonexistentword") == []


async def test_full_text_search_is_scoped_to_a_user_and_its_tags(migrated_url):
    service = MemoryService(migrated_url)
    await _store(migrated_url, "mine", user_id="usr_a", content="shared subject", tags=["work"])
    await _store(migrated_url, "theirs", user_id="usr_b", content="shared subject", tags=["work"])
    await _store(migrated_url, "untagged", user_id="usr_a", content="shared subject")

    assert {row["id"] for row in await service.full_text("shared", user_id="usr_a")} == {"mine", "untagged"}
    assert {row["id"] for row in await service.full_text("shared", user_id="usr_a", tags=["work"])} == {"mine"}
    assert len(await service.full_text("shared", limit=1)) == 1
