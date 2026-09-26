"""The audit log on the repository/service layer, on SQLite and Postgres."""

from __future__ import annotations

import json

import pytest

from pincer.db.engine import get_engine
from pincer.models.audit import AuditLog
from pincer.security.audit import AuditAction, AuditEntry
from pincer.services.audit import MAX_SUMMARY_LENGTH, AuditService

_TABLES = [AuditLog.__table__]


@pytest.fixture
async def url(db_url: str):
    engine = get_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: AuditLog.metadata.drop_all(c, tables=_TABLES))
        await conn.run_sync(lambda c: AuditLog.metadata.create_all(c, tables=_TABLES))
    yield db_url
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: AuditLog.metadata.drop_all(c, tables=_TABLES))


def _entry(**kwargs) -> AuditEntry:
    fields = {"user_id": "usr_a", "action": AuditAction.TOOL_CALL, "tool": "shell_exec"}
    return AuditEntry(**{**fields, **kwargs})


async def test_a_batch_is_stored_as_the_row_shape_the_schema_expects(url):
    service = AuditService(url)
    await service.add_batch(
        [
            _entry(timestamp="2026-01-01T10:00:00+00:00", metadata={"cmd": "ls"}, cost_usd=0.5),
            _entry(timestamp="2026-01-01T11:00:00+00:00", approved=False),
        ]
    )

    rows = await service.query()
    assert [row["timestamp"] for row in rows] == [  # newest first
        "2026-01-01T11:00:00+00:00",
        "2026-01-01T10:00:00+00:00",
    ]
    newest, oldest = rows
    assert newest["approved"] == 0  # the column holds 0/1, not a boolean
    assert oldest["approved"] == 1
    assert oldest["action"] == "tool_call"  # the enum's value
    assert json.loads(oldest["metadata_json"]) == {"cmd": "ls"}
    assert newest["metadata_json"] is None
    assert oldest["cost_usd"] == pytest.approx(0.5)


async def test_long_summaries_are_capped(url):
    service = AuditService(url)
    await service.add_batch([_entry(input_summary="x" * (MAX_SUMMARY_LENGTH + 500))])
    assert len((await service.query())[0]["input_summary"]) == MAX_SUMMARY_LENGTH


async def test_one_unstorable_entry_does_not_block_the_batch(url):
    """The caller re-queues a failed batch, so a single bad entry would
    otherwise stop audit persistence for good."""
    service = AuditService(url)
    await service.add_batch(
        [
            _entry(timestamp="2026-01-01T00:00:00+00:00", metadata={"obj": object()}),
            _entry(timestamp="2026-01-02T00:00:00+00:00"),
        ]
    )
    stored = await service.query()
    assert len(stored) == 2
    # The exotic value is stringified rather than failing the write.
    assert "object object at" in json.loads(stored[1]["metadata_json"])["obj"]


async def test_an_entry_with_no_action_is_dropped_not_re_raised(url):
    """The drop's own log line read `entry.action`, so an entry missing it
    failed the whole batch — which the caller re-queues, forever."""
    service = AuditService(url)
    await service.add_batch([object(), _entry(timestamp="2026-01-02T00:00:00+00:00")])
    assert len(await service.query()) == 1


async def test_filters_and_paging(url):
    service = AuditService(url)
    await service.add_batch(
        [
            _entry(timestamp="2026-01-01T00:00:00+00:00", user_id="usr_a", tool="shell_exec"),
            _entry(timestamp="2026-01-02T00:00:00+00:00", user_id="usr_b", tool="file_write"),
            _entry(timestamp="2026-01-03T00:00:00+00:00", user_id="usr_a", action=AuditAction.LLM_REQUEST, tool=None),
        ]
    )

    assert await service.count(user_id="usr_a") == 2
    assert await service.count(action="llm_request") == 1
    assert await service.count(tool="file_write") == 1
    assert await service.count(since="2026-01-02T00:00:00+00:00") == 2
    assert await service.count(until="2026-01-02T00:00:00+00:00") == 2
    assert await service.count(since="2026-01-02T00:00:00+00:00", until="2026-01-02T00:00:00+00:00") == 1

    page = await service.query(limit=1, offset=1)
    assert [row["timestamp"] for row in page] == ["2026-01-02T00:00:00+00:00"]


async def test_stats_summarise_the_window(url):
    service = AuditService(url)
    await service.add_batch(
        [
            _entry(timestamp="2026-01-01T00:00:00+00:00", cost_usd=1.0),
            _entry(timestamp="2026-01-02T00:00:00+00:00", cost_usd=2.0, approved=False),
            _entry(timestamp="2026-01-03T00:00:00+00:00", action=AuditAction.LLM_REQUEST, tool=None, cost_usd=4.0),
        ]
    )

    stats = await service.stats()
    assert stats["total_entries"] == 3
    assert stats["by_action"] == {"tool_call": 2, "llm_request": 1}
    assert stats["by_tool"] == {"shell_exec": 2}  # the NULL tool is left out
    assert stats["total_cost_usd"] == pytest.approx(7.0)
    assert stats["failed_actions"] == 1

    windowed = await service.stats(since="2026-01-02T00:00:00+00:00")
    assert windowed["total_entries"] == 2
    assert windowed["total_cost_usd"] == pytest.approx(6.0)


async def test_stats_on_an_empty_log(url):
    stats = await AuditService(url).stats()
    assert stats == {
        "total_entries": 0,
        "by_action": {},
        "by_tool": {},
        "total_cost_usd": 0.0,
        "failed_actions": 0,
    }


async def test_export_writes_every_matching_entry_oldest_first(url, tmp_path):
    service = AuditService(url)
    await service.add_batch(
        [
            _entry(timestamp="2026-01-02T00:00:00+00:00", metadata={"n": 2}),
            _entry(timestamp="2026-01-01T00:00:00+00:00", metadata={"n": 1}),
            _entry(timestamp="2026-01-03T00:00:00+00:00", user_id="usr_b"),
        ]
    )

    out = tmp_path / "audit.json"
    count = await service.export_json(out, user_id="usr_a")

    assert count == 2
    records = json.loads(out.read_text())
    assert [r["timestamp"] for r in records] == ["2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00"]
    # metadata_json is decoded back into an object, and not left behind as text.
    assert records[0]["metadata"] == {"n": 1}
    assert "metadata_json" not in records[0]


async def test_export_of_nothing_is_still_valid_json(url, tmp_path):
    out = tmp_path / "audit.json"
    assert await AuditService(url).export_json(out) == 0
    assert json.loads(out.read_text()) == []
