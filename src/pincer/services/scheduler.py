"""Scheduling: cron schedules, event-trigger deduplication, briefing settings.

Each method is one unit of work on the configured database. Timestamps written
here keep SQLite's `datetime('now')` shape (`YYYY-MM-DD HH:MM:SS`, UTC), which
is what the existing rows and the `CURRENT_TIMESTAMP` column defaults hold.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any
from zoneinfo import ZoneInfo

from croniter import croniter
from fastapi import Depends

from pincer.db.session import session_scope
from pincer.models.scheduler import BriefingConfig, Schedule
from pincer.repositories.scheduler import (
    BriefingConfigRepository,
    EventTriggerRepository,
    ScheduleRepository,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

#: What a briefing looks like before the owner customises it — the column
#: defaults from migration 0001, returned for a row this process just created.
DEFAULT_BRIEFING_CONFIG: dict[str, Any] = {
    "sections": '["weather","calendar","email","news"]',
    "custom_sections": "[]",
    "weather_location": "Berlin,DE",
    "news_topics": '["technology","business"]',
}

_EDITABLE_BRIEFING_FIELDS = frozenset({"sections", "custom_sections", "weather_location", "news_topics"})


def _sql_now() -> str:
    """UTC in SQLite's `datetime('now')` format, which these columns hold."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _next_run_utc(cron_expr: str, tz: str) -> str:
    return croniter(cron_expr, datetime.now(ZoneInfo(tz))).get_next(datetime).astimezone(UTC).isoformat()


class DatabaseService:
    """Base for services bound to one database URL and nothing else."""

    def __init__(self, url: str | None = None) -> None:
        self._url = url

    @classmethod
    async def for_path(cls, db_path: Path) -> Any:
        """A service on `db_path`, brought to head first."""
        import asyncio

        from pincer.db.engine import ensure_schema_current, get_database_url

        await asyncio.to_thread(ensure_schema_current, db_path)
        return cls(get_database_url(db_path))


class ScheduleService(DatabaseService):
    """CRUD and the due-schedule query for cron schedules."""

    async def add(
        self,
        name: str,
        cron_expr: str,
        action: dict[str, Any],
        pincer_user_id: str,
        tz: str = "UTC",
        channel: str = "telegram",
    ) -> int:
        if not croniter.is_valid(cron_expr):
            raise ValueError(f"Invalid cron expression: {cron_expr}")
        try:
            next_run_at = _next_run_utc(cron_expr, tz)
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"Invalid timezone: {tz}") from e

        async with session_scope(self._url) as session:
            row = await ScheduleRepository(session).add(
                Schedule(
                    pincer_user_id=pincer_user_id,
                    name=name,
                    cron_expr=cron_expr,
                    action=json.dumps(action),
                    channel=channel,
                    timezone=tz,
                    next_run_at=next_run_at,
                )
            )
            schedule_id = int(row.id or 0)
        logger.info("Schedule added: %s (cron=%s, tz=%s)", name, cron_expr, tz)
        return schedule_id

    async def remove(self, schedule_id: int, pincer_user_id: str) -> bool:
        async with session_scope(self._url) as session:
            removed = await ScheduleRepository(session).delete_for_user(schedule_id, pincer_user_id)
        if removed:
            logger.info("Schedule removed: id=%s", schedule_id)
        else:
            logger.warning("Schedule remove no-op: id=%s not found for user", schedule_id)
        return bool(removed)

    async def toggle(self, schedule_id: int, enabled: bool, pincer_user_id: str) -> bool:
        async with session_scope(self._url) as session:
            changed = await ScheduleRepository(session).set_enabled(
                schedule_id, enabled, pincer_user_id, now=_sql_now()
            )
        if changed:
            logger.info("Schedule %s: id=%s", "enabled" if enabled else "disabled", schedule_id)
        else:
            logger.warning("Schedule toggle no-op: id=%s not found for user", schedule_id)
        return bool(changed)

    async def list_for_user(self, pincer_user_id: str) -> list[dict[str, Any]]:
        async with session_scope(self._url) as session:
            rows = await ScheduleRepository(session).for_user(pincer_user_id)
        return [_as_row(row) for row in rows]

    async def list_all(self) -> list[dict[str, Any]]:
        """Every schedule, unordered — the caller classifies and sorts."""
        async with session_scope(self._url) as session:
            rows = await ScheduleRepository(session).list()
        return [_as_row(row) for row in rows]

    async def get(self, schedule_id: int) -> dict[str, Any] | None:
        async with session_scope(self._url) as session:
            row = await ScheduleRepository(session).get(schedule_id)
        if row is None:
            logger.debug("Schedule lookup miss: id=%s", schedule_id)
            return None
        return _as_row(row)

    async def due(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now_utc = (now or datetime.now(UTC)).isoformat()
        async with session_scope(self._url) as session:
            rows = await ScheduleRepository(session).due(now_utc)
        logger.debug("Due-schedule query: %d due as of %s", len(rows), now_utc)
        return [_as_row(row) for row in rows]

    async def mark_fired(self, schedule_id: int, next_run_at: str) -> None:
        async with session_scope(self._url) as session:
            await ScheduleRepository(session).mark_fired(schedule_id, next_run_at=next_run_at, now=_sql_now())
        logger.debug("Schedule marked fired: id=%s next_run_at=%s", schedule_id, next_run_at)


class EventTriggerService(DatabaseService):
    """Deduplication for event-driven triggers (one row per event seen)."""

    async def is_processed(self, trigger_type: str, trigger_key: str) -> bool:
        async with session_scope(self._url) as session:
            return await EventTriggerRepository(session).is_processed(trigger_type, trigger_key)

    async def mark_processed(self, trigger_type: str, trigger_key: str, user_id: str, result: str = "") -> None:
        async with session_scope(self._url) as session:
            await EventTriggerRepository(session).mark_processed(trigger_type, trigger_key, user_id, result)


class BriefingConfigService(DatabaseService):
    """Per-user morning-briefing settings."""

    async def get_or_create(self, pincer_user_id: str) -> dict[str, Any]:
        async with session_scope(self._url) as session:
            repo = BriefingConfigRepository(session)
            existing = await repo.for_user(pincer_user_id)
            if existing is not None:
                return _as_row(existing)
            await repo.add(BriefingConfig(pincer_user_id=pincer_user_id), refresh=False)
        return dict(DEFAULT_BRIEFING_CONFIG)

    async def update(self, pincer_user_id: str, **kwargs: Any) -> None:
        values = {k: v for k, v in kwargs.items() if k in _EDITABLE_BRIEFING_FIELDS}
        if not values:
            return
        async with session_scope(self._url) as session:
            await BriefingConfigRepository(session).update_for_user(pincer_user_id, values, now=_sql_now())


def _as_row(row: Any) -> dict[str, Any]:
    """A model as the plain dict the existing callers and API expect."""
    return {name: getattr(row, name) for name in row.__class__.model_fields}


async def get_schedule_service() -> ScheduleService:
    """FastAPI dependency: schedules on the configured database."""
    from pincer.config import get_settings_relaxed

    service: ScheduleService = await ScheduleService.for_path(get_settings_relaxed().db_path)
    return service


ScheduleServiceDep = Annotated[ScheduleService, Depends(get_schedule_service)]
