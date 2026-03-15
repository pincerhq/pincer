"""
Proactive agent — morning briefing and custom actions.

Generates a daily briefing with:
1. Weather (OpenWeatherMap API)
2. Calendar (reuses tools/builtin/calendar_tool.py)
3. Email (reuses tools/builtin/email_tool.py)
4. News (NewsAPI)

Users customize via briefing_config table.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

import aiosqlite
import httpx

from pincer.config import get_settings

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class ProactiveAgent:
    """Generates proactive messages — briefings, notifications."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = str(db_path)
        self._http = httpx.AsyncClient(timeout=15)

    async def close(self) -> None:
        await self._http.aclose()

    async def ensure_table(self) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS briefing_config (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pincer_user_id TEXT NOT NULL UNIQUE,
                    sections TEXT NOT NULL DEFAULT '["weather","calendar","email","news"]',
                    custom_sections TEXT DEFAULT '[]',
                    weather_location TEXT DEFAULT 'Berlin,DE',
                    news_topics TEXT DEFAULT '["technology","business"]',
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """)
            await db.commit()

    # ── Main briefing generator ──────────────────

    async def generate_briefing(
        self,
        pincer_user_id: str,
        action: dict[str, Any] | None = None,
        channel: str = "telegram",
    ) -> str:
        config = await self._get_briefing_config(pincer_user_id)
        sections = json.loads(config.get("sections", '["weather","calendar","email","news"]'))

        parts = [f"Good morning! Briefing for {datetime.now().strftime('%A, %B %d')}:\n"]

        section_builders = {
            "weather": lambda: self._weather(config.get("weather_location", "Berlin,DE")),
            "calendar": self._calendar,
            "email": self._email,
            "news": lambda: self._news(
                json.loads(config.get("news_topics", '["technology"]')),
            ),
        }

        for section in sections:
            try:
                builder = section_builders.get(section)
                if builder:
                    parts.append(await builder())
            except Exception:
                logger.exception("Briefing section '%s' failed", section)
                parts.append(f"[{section.title()}]: Unavailable\n")

        parts.append("---\nHave a great day!")
        return "\n".join(parts)

    async def _weather(self, location: str) -> str:
        settings = get_settings()
        api_key = settings.openweathermap_api_key.get_secret_value()
        if not api_key:
            return "Weather: API key not configured\n"
        try:
            resp = await self._http.get(
                "https://api.openweathermap.org/data/2.5/weather",
                params={"q": location, "appid": api_key, "units": "metric"},
            )
            resp.raise_for_status()
            d = resp.json()
            return (
                f"Weather — {location}\n"
                f"  {d['weather'][0]['description'].capitalize()}, "
                f"{d['main']['temp']:.0f}C "
                f"(feels {d['main']['feels_like']:.0f}C)\n"
                f"  Humidity: {d['main']['humidity']}% | Wind: {d['wind']['speed']} m/s\n"
            )
        except Exception as e:
            logger.error("Briefing weather failed: %s", e)
            return "Weather: Unavailable\n"

    async def _calendar(self) -> str:
        try:
            from pincer.tools.builtin.calendar_tool import calendar_today

            result = await calendar_today()
            if isinstance(result, str):
                return f"Calendar\n{result}\n"
            return "Calendar: No data\n"
        except Exception as e:
            logger.error("Briefing calendar failed: %s", e)
            return "Calendar: Unavailable\n"

    async def _email(self) -> str:
        try:
            from pincer.tools.builtin.email_tool import email_check

            result = await email_check(limit=3)
            if isinstance(result, str):
                return f"Email\n{result}\n"
            return "Email: No data\n"
        except Exception as e:
            logger.error("Briefing email failed: %s", e)
            return "Email: Unavailable\n"

    async def _news(self, topics: list[str]) -> str:
        settings = get_settings()
        api_key = settings.newsapi_key.get_secret_value()
        if not api_key:
            return "News: API key not configured\n"
        try:
            resp = await self._http.get(
                "https://newsapi.org/v2/top-headlines",
                params={
                    "q": " OR ".join(topics),
                    "language": "en",
                    "pageSize": 5,
                    "apiKey": api_key,
                },
            )
            resp.raise_for_status()
            articles = resp.json().get("articles", [])
            if not articles:
                return "News — No headlines\n"
            lines = ["News — Headlines:"]
            for a in articles[:5]:
                if a.get("title"):
                    source = a.get("source", {}).get("name", "")
                    lines.append(f"  - {a['title']} ({source})")
            return "\n".join(lines) + "\n"
        except Exception as e:
            logger.error("Briefing news failed: %s", e)
            return "News: Unavailable\n"

    # ── Config management ────────────────────────

    async def _get_briefing_config(self, pincer_user_id: str) -> dict[str, Any]:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                "SELECT * FROM briefing_config WHERE pincer_user_id = ?",
                (pincer_user_id,),
            )
            if rows:
                return dict(rows[0])
            await db.execute(
                "INSERT INTO briefing_config (pincer_user_id) VALUES (?)",
                (pincer_user_id,),
            )
            await db.commit()
            return {
                "sections": '["weather","calendar","email","news"]',
                "custom_sections": "[]",
                "weather_location": "Berlin,DE",
                "news_topics": '["technology","business"]',
            }

    async def update_briefing_config(
        self,
        pincer_user_id: str,
        **kwargs: Any,
    ) -> None:
        valid = {"sections", "custom_sections", "weather_location", "news_topics"}
        updates = {k: v for k, v in kwargs.items() if k in valid}
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [pincer_user_id]
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                f"UPDATE briefing_config SET {set_clause}, "  # noqa: S608
                "updated_at = datetime('now') WHERE pincer_user_id = ?",
                values,
            )
            await db.commit()

    # ── Custom action handler (for scheduler) ────

    async def run_custom_action(
        self,
        pincer_user_id: str,
        action: dict[str, Any],
        channel: str = "telegram",
    ) -> str:
        prompt = action.get("prompt", "")
        if not prompt:
            return "Custom action has no prompt configured."
        return f"Custom action: {prompt}"
