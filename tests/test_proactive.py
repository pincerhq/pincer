"""Tests for proactive agent (morning briefing)."""

from unittest.mock import patch

import pytest
import pytest_asyncio

from pincer.scheduler.proactive import ProactiveAgent


@pytest_asyncio.fixture
async def proactive(tmp_path):
    agent = ProactiveAgent(tmp_path / "pincer.db")
    await agent.ensure_table()
    yield agent
    await agent.close()


@pytest.mark.asyncio
class TestProactiveAgent:
    async def test_generate_briefing_default(self, proactive):
        """Briefing should generate even when external services are unavailable."""
        with (
            patch.object(proactive, "_weather", return_value="Weather: Clear, 20C\n"),
            patch.object(proactive, "_calendar", return_value="Calendar: No events\n"),
            patch.object(proactive, "_email", return_value="Email: No unread\n"),
            patch.object(proactive, "_news", return_value="News: No headlines\n"),
        ):
            result = await proactive.generate_briefing("usr_test")
            assert "Good morning" in result
            assert "Weather" in result
            assert "Calendar" in result
            assert "Email" in result
            assert "News" in result

    async def test_briefing_config_creation(self, proactive):
        """First call should create default config."""
        config = await proactive._get_briefing_config("usr_new")
        assert "weather" in config["sections"]
        assert "Berlin" in config["weather_location"]

    async def test_update_briefing_config(self, proactive):
        await proactive._get_briefing_config("usr_test")
        await proactive.update_briefing_config(
            "usr_test",
            weather_location="London,UK",
        )
        config = await proactive._get_briefing_config("usr_test")
        assert config["weather_location"] == "London,UK"

    async def test_invalid_config_key_ignored(self, proactive):
        await proactive._get_briefing_config("usr_test")
        await proactive.update_briefing_config(
            "usr_test",
            invalid_field="should_be_ignored",
        )

    async def test_custom_action(self, proactive):
        result = await proactive.run_custom_action(
            "usr_test",
            {"type": "custom", "prompt": "Check stock prices"},
        )
        assert "stock prices" in result.lower()

    async def test_custom_action_no_prompt(self, proactive):
        result = await proactive.run_custom_action(
            "usr_test",
            {"type": "custom"},
        )
        assert "no prompt" in result.lower()

    async def test_weather_no_api_key(self, proactive):
        with patch("pincer.scheduler.proactive.get_settings") as mock_s:
            mock_s.return_value.openweathermap_api_key.get_secret_value.return_value = ""
            result = await proactive._weather("Berlin,DE")
            assert "not configured" in result.lower()

    async def test_news_no_api_key(self, proactive):
        with patch("pincer.scheduler.proactive.get_settings") as mock_s:
            mock_s.return_value.newsapi_key.get_secret_value.return_value = ""
            result = await proactive._news(["technology"])
            assert "not configured" in result.lower()
