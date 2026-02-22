"""Shared test fixtures."""

import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

# Set test env vars before importing config
os.environ["PINCER_ANTHROPIC_API_KEY"] = "sk-ant-test-key"
os.environ["PINCER_TELEGRAM_BOT_TOKEN"] = "123456:TEST"
os.environ["PINCER_DATA_DIR"] = "/tmp/pincer-test"
os.environ["PINCER_DAILY_BUDGET_USD"] = "100.0"

from pincer.config import Settings
from pincer.core.session import SessionManager
from pincer.llm.base import BaseLLMProvider, LLMResponse
from pincer.llm.cost_tracker import CostTracker
from pincer.tools.registry import ToolRegistry


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        anthropic_api_key="sk-ant-test-key",  # type: ignore[arg-type]
        telegram_bot_token="123456:TEST",  # type: ignore[arg-type]
        data_dir=tmp_path / ".pincer",
        daily_budget_usd=100.0,
    )


@pytest.fixture
def mock_llm() -> AsyncMock:
    llm = AsyncMock(spec=BaseLLMProvider)
    llm.complete.return_value = LLMResponse(
        content="Hello! I'm Pincer.",
        model="test-model",
        input_tokens=100,
        output_tokens=50,
        stop_reason="end_turn",
    )
    llm.close.return_value = None
    return llm


@pytest_asyncio.fixture
async def session_manager(tmp_path: Path) -> SessionManager:
    sm = SessionManager(tmp_path / "test.db", max_messages=20)
    await sm.initialize()
    yield sm  # type: ignore[misc]
    await sm.close()


@pytest_asyncio.fixture
async def cost_tracker(tmp_path: Path) -> CostTracker:
    ct = CostTracker(tmp_path / "test.db", daily_budget=100.0)
    await ct.initialize()
    yield ct  # type: ignore[misc]
    await ct.close()


@pytest.fixture
def tool_registry() -> ToolRegistry:
    registry = ToolRegistry()

    async def greet(name: str) -> str:
        return f"Hello, {name}!"

    registry.register(
        name="greet",
        description="Greet someone",
        handler=greet,
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    )
    return registry


# ── Sprint 3 fixtures ────────────────────────────

@pytest_asyncio.fixture
async def identity_resolver(tmp_path: Path):
    from pincer.core.identity import IdentityResolver

    db_path = tmp_path / "identity.db"
    resolver = IdentityResolver(db_path, identity_map_config="")
    await resolver.ensure_table()
    return resolver


@pytest_asyncio.fixture
async def channel_router(tmp_path: Path):
    from unittest.mock import AsyncMock as _AM

    from pincer.channels.router import ChannelRouter
    from pincer.core.identity import IdentityResolver

    db_path = tmp_path / "router.db"
    identity = IdentityResolver(db_path, identity_map_config="")
    await identity.ensure_table()
    return ChannelRouter(identity)
