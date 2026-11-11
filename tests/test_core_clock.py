"""Tests for the current-date-and-time context injected into every prompt.

The model has no clock; if nothing states the date it invents one. These cover
the resolution order (per-user > deployment > default), the rendering, and the
fact that every system-prompt path actually carries the block.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from pincer.core.clock import DEFAULT_TIMEZONE, current_time_block, resolve_timezone

FIXED = datetime(2026, 9, 13, 10, 41, tzinfo=UTC)


def _settings(timezone: str = "Europe/Berlin"):
    return SimpleNamespace(timezone=timezone)


class TestResolveTimezone:
    def test_override_wins_over_settings(self):
        assert resolve_timezone(_settings("Europe/Berlin"), "Europe/Kyiv").key == "Europe/Kyiv"

    def test_settings_used_without_override(self):
        assert resolve_timezone(_settings("Asia/Tokyo")).key == "Asia/Tokyo"

    def test_falls_back_to_default_when_unset(self):
        assert resolve_timezone(_settings("")).key == DEFAULT_TIMEZONE

    def test_unknown_override_falls_through_to_settings(self):
        """A typo in one user's timezone must not drag the whole turn down to
        the hardcoded default — the deployment setting is still better."""
        assert resolve_timezone(_settings("Asia/Tokyo"), "Mars/Olympus").key == "Asia/Tokyo"

    def test_unknown_everywhere_still_returns_a_zone(self):
        assert resolve_timezone(_settings("Nope/Nope"), "Also/Nope").key == DEFAULT_TIMEZONE


class TestCurrentTimeBlock:
    def test_renders_user_local_time_not_utc(self):
        block = current_time_block(_settings("Europe/Berlin"), now=FIXED)
        assert "Sunday, 13 September 2026, 12:41" in block  # 10:41 UTC = 12:41 CEST
        assert "Europe/Berlin" in block

    def test_per_user_override_shifts_the_clock(self):
        block = current_time_block(_settings("Europe/Berlin"), "Europe/Kyiv", now=FIXED)
        assert "13:41" in block
        assert "Europe/Kyiv" in block

    def test_dst_is_applied_not_a_fixed_offset(self):
        winter = current_time_block(_settings("Europe/Berlin"), now=datetime(2026, 1, 15, 10, 41, tzinfo=UTC))
        assert "11:41" in winter
        assert "CET" in winter

    def test_date_rolls_over_with_the_zone(self):
        """23:30 UTC is already tomorrow in Berlin — the whole point of
        rendering in the user's zone rather than the server's."""
        block = current_time_block(_settings("Europe/Berlin"), now=datetime(2026, 1, 15, 23, 30, tzinfo=UTC))
        assert "Friday, 16 January 2026, 00:30" in block

    def test_month_is_named_never_numeric(self):
        """'03/04' reads as two different dates depending on locale."""
        block = current_time_block(_settings("UTC"), now=datetime(2026, 4, 3, 9, 0, tzinfo=UTC))
        assert "03 April 2026" in block

    def test_instructs_the_model_not_to_guess(self):
        block = current_time_block(_settings(), now=FIXED)
        assert "training data is not a clock" in block


class TestAgentPromptInjection:
    @staticmethod
    def _agent(**kwargs):
        from pincer.core.agent import Agent

        settings = MagicMock()
        settings.system_prompt = "You are helpful."
        settings.timezone = kwargs.pop("timezone", "Europe/Berlin")
        settings.mcp_instructions_max_chars = 400
        tool_registry = MagicMock()
        tool_registry.list_tools = MagicMock(return_value=[])
        return Agent(
            settings=settings,
            llm=MagicMock(),
            session_manager=MagicMock(),
            cost_tracker=MagicMock(),
            tool_registry=tool_registry,
            **kwargs,
        )

    @staticmethod
    async def _context(agent, text: str = "hi") -> str:
        """The turn's context as a turn builds it: the user's zone resolved once, then the context."""
        turn = await agent._begin_turn("user1")
        return await agent._turn_context("user1", text, memory=True, user_timezone=turn.timezone)

    async def test_the_time_rides_on_the_user_message_not_the_system_prompt(self):
        """The system prompt stays identical for a conversation (issue #357, Q14)."""
        agent = self._agent()
        assert (await self._context(agent)).startswith("[Current date and time]")
        assert "[Current date and time]" not in await agent._build_system_prompt("user1")

    async def test_memory_path_carries_the_time(self):
        memory = MagicMock()
        memory.search_text = AsyncMock(return_value=[SimpleNamespace(content="likes tea")])
        agent = self._agent(memory_store=memory)
        context = await self._context(agent, "hello")
        assert "likes tea" in context
        assert "[Current date and time]" in context

    async def test_extra_system_stays_in_the_prompt_without_the_time(self):
        agent = self._agent()
        prompt = await agent._build_system_prompt("user1", extra_system="EXTRA")
        assert prompt.endswith("EXTRA")
        assert "[Current date and time]" not in prompt

    async def test_time_block_is_last(self):
        """It should be the final thing the model reads before the user's words."""
        memory = MagicMock()
        memory.search_text = AsyncMock(return_value=[SimpleNamespace(content="likes tea")])
        agent = self._agent(memory_store=memory)
        context = await self._context(agent)
        assert context.index("[Current date and time]") > context.index("likes tea")

    async def test_uses_the_users_own_timezone(self):
        agent = self._agent(timezone="Europe/Berlin")
        agent.identity_resolver = MagicMock()
        agent.identity_resolver.get_timezone = AsyncMock(return_value="Asia/Tokyo")
        assert "Asia/Tokyo" in await self._context(agent)

    async def test_identity_lookup_failure_falls_back_quietly(self):
        """A locked or missing identity DB must cost the user their timezone,
        not their answer."""
        agent = self._agent(timezone="Europe/Berlin")
        agent.identity_resolver = MagicMock()
        agent.identity_resolver.get_timezone = AsyncMock(side_effect=RuntimeError("db locked"))
        assert "Europe/Berlin" in await self._context(agent)

    async def test_no_resolver_uses_deployment_timezone(self):
        agent = self._agent(timezone="Asia/Tokyo")
        assert "Asia/Tokyo" in await self._context(agent)
