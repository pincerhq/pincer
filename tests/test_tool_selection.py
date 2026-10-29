"""Tests for ToolSelector — per-turn tool budget with RAG ranking on the memory MCP server."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from pincer.tools.selection import ToolSelector

if TYPE_CHECKING:
    from pincer.config import Settings
    from pincer.mcp.manager import MCPClientManager

MEMORY_TOOLS = [f"sqlite_vec_memory__memory_{op}" for op in ("store", "search", "list", "get", "delete", "update")]
HEAD = ["make_phone_call", "send_file", "schedule_list", *MEMORY_TOOLS]


@dataclass
class FakeResult:
    structuredContent: dict[str, Any]  # noqa: N815
    isError: bool = False  # noqa: N815
    content: list[Any] = field(default_factory=list)


class FakeIndexSession:
    """In-memory imitation of the server's tool_index_sync / tool_index_search."""

    def __init__(self, ranking: list[str] | None = None, *, has_index: bool = True) -> None:
        self.ranking = ranking or []
        index_tools = ["tool_index_sync", "tool_index_search"] if has_index else []
        self.tools = [SimpleNamespace(name=n) for n in ["memory_store", *index_tools]]
        self.version = ""
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail: Exception | None = None
        self.search_delay = 0.0
        self.sync_gate: asyncio.Event | None = None

    async def call_tool(self, name: str, args: dict[str, Any]) -> FakeResult:
        self.calls.append((name, args))
        if self.fail is not None:
            raise self.fail
        if name == "tool_index_sync":
            if self.sync_gate is not None:
                await self.sync_gate.wait()
            self.version = args["version"]
            return FakeResult({"version": self.version, "added": len(args["tools"])})
        await asyncio.sleep(self.search_delay)
        if args["version"] != self.version:
            return FakeResult({"stale": True, "results": []})
        return FakeResult(
            {"stale": False, "results": [{"name": n, "distance": 0.1} for n in self.ranking[: args["k"]]]}
        )

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)


class FakeManager:
    def __init__(self, session: FakeIndexSession, memory_tools: list[str] = MEMORY_TOOLS) -> None:
        self.session = session
        self.memory_tools = memory_tools
        self.connected = True

    async def get_session(self, name: str) -> FakeIndexSession:
        assert name == "sqlite_vec_memory"
        if not self.connected:
            raise ValueError("MCP server 'sqlite_vec_memory' not connected. Available: []")
        return self.session

    def tool_names(self, name: str) -> list[str]:
        return self.memory_tools if name == "sqlite_vec_memory" and self.connected else []


def _selector(manager: FakeManager | None, settings: Settings) -> ToolSelector:
    return ToolSelector(cast("MCPClientManager | None", manager), settings)


def _schemas(names: list[str]) -> list[dict[str, Any]]:
    return [{"name": n, "description": f"{n} tool", "input_schema": {"type": "object"}} for n in names]


def _registry_names() -> list[str]:
    """189 tools with make_phone_call at index 141, like the production instance."""
    names = [f"filler_{i:03d}" for i in range(189 - 1 - len(MEMORY_TOOLS) - 2)]
    names = names[:141] + ["make_phone_call"] + names[141:]
    return names + ["send_file", "schedule_list"] + MEMORY_TOOLS


def _names(schemas: list[dict[str, Any]]) -> list[str]:
    return [s["name"] for s in schemas]


def _fallback_logs(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith("Tool selection without ranking")]


@pytest.fixture
def openai_settings(settings: Settings) -> Settings:
    """The shared settings with an OpenAI-wire (128-tool capped) default provider."""
    return settings.model_copy(update={"default_provider": "openai"})


async def test_at_or_below_limit_returns_schemas_unchanged(openai_settings: Settings) -> None:
    session = FakeIndexSession()
    schemas = _schemas([f"t{i}" for i in range(127)])
    assert await _selector(FakeManager(session), openai_settings).select(schemas, "hello") is schemas
    assert session.calls == []


async def test_uncapped_providers_get_every_tool(settings: Settings) -> None:
    """Anthropic has no tool cap and caches the tools block as a static prefix: no selection."""
    session = FakeIndexSession(["filler_150"])
    selector = _selector(FakeManager(session), settings)
    schemas = _schemas(_registry_names())
    selector.start_sync(schemas)

    assert await selector.select(schemas, "Ruf mich an") is schemas
    await asyncio.sleep(0)
    assert session.calls == []


async def test_an_openai_failover_turns_selection_on(settings: Settings) -> None:
    settings = settings.model_copy(update={"fallback_providers": ["openai"]})
    selected = await _selector(FakeManager(FakeIndexSession(["filler_150"])), settings).select(
        _schemas(_registry_names()), "x"
    )
    assert len(selected) == 127


async def test_selection_tiers_budget_and_registry_order(openai_settings: Settings) -> None:
    names = _registry_names()
    ranking = ["filler_150", "make_phone_call", "filler_007", "slack__unknown"] + [
        f"filler_{i:03d}" for i in range(180)
    ]
    session = FakeIndexSession(ranking)

    selected = _names(
        await _selector(FakeManager(session), openai_settings).select(_schemas(names), "Ruf mich an", ["filler_100"])
    )

    assert len(selected) == 127
    assert selected == [n for n in names if n in set(selected)]  # registry order: a stable set is a stable prefix
    assert set(HEAD) | {"filler_100", "filler_150", "filler_007"} <= set(selected)
    assert "slack__unknown" not in selected
    # Everything past the head comes from the ranking: the 180 fillers fill 127 - 10 slots in rank order.
    assert {"filler_179", "filler_178"}.isdisjoint(selected)
    sync_args = next(args for name, args in session.calls if name == "tool_index_sync")
    assert {t["name"] for t in sync_args["tools"]} == set(names)
    search_args = next(args for name, args in session.calls if name == "tool_index_search")
    assert search_args["k"] == 127


async def test_default_list_override_replaces_head(openai_settings: Settings) -> None:
    settings = openai_settings.model_copy(update={"tool_selection_default": ["filler_00*"]})
    # The ranking only names tools past the budget, so everything else comes from the head.
    selector = _selector(FakeManager(FakeIndexSession(["filler_150"]), memory_tools=[]), settings)

    selected = _names(await selector.select(_schemas(_registry_names()), "x"))

    assert [n for n in selected if n.startswith("filler_00")] == [f"filler_00{i}" for i in range(10)]
    assert "filler_150" in selected
    assert "make_phone_call" not in selected


def test_default_list_parses_comma_separated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from pincer.config import Settings

    monkeypatch.setenv("PINCER_TOOL_SELECTION_DEFAULT", "send_file, schedule_*")
    s = Settings(anthropic_api_key="sk-ant-test-key", data_dir=tmp_path)  # type: ignore[arg-type]
    assert s.tool_selection_default == ["send_file", "schedule_*"]


async def test_index_synced_once_per_registry_version(openai_settings: Settings) -> None:
    session = FakeIndexSession(["filler_150"])
    selector = _selector(FakeManager(session), openai_settings)
    schemas = _schemas(_registry_names())

    await selector.select(schemas, "a")
    await selector.select(schemas, "b")
    assert session.count("tool_index_sync") == 1

    await selector.select([*schemas, *_schemas(["new_tool"])], "c")
    assert session.count("tool_index_sync") == 2


async def test_stale_search_resyncs_and_retries(openai_settings: Settings) -> None:
    session = FakeIndexSession(["filler_150"])
    selector = _selector(FakeManager(session), openai_settings)
    schemas = _schemas(_registry_names())
    await selector.select(schemas, "a")

    session.version = ""  # memory server restarted and lost its in-memory index
    selected = _names(await selector.select(schemas, "b"))

    assert session.count("tool_index_sync") == 2
    assert "filler_150" in selected


@pytest.mark.parametrize("failure", ["error", "timeout"])
async def test_failure_falls_back_to_head_then_registry_order(
    openai_settings: Settings, failure: str, caplog: pytest.LogCaptureFixture
) -> None:
    session = FakeIndexSession(["filler_150"])
    settings = openai_settings
    if failure == "error":
        session.fail = RuntimeError("no such table: tool_index")
    else:
        session.search_delay = 1.0
        settings = openai_settings.model_copy(update={"tool_selection_timeout": 0.05})
    names = _registry_names()

    with caplog.at_level(logging.DEBUG, logger="pincer.tools.selection"):
        selected = _names(await _selector(FakeManager(session), settings).select(_schemas(names), "Ruf mich an"))

    rest = [n for n in names if n not in HEAD][: 127 - len(HEAD)]
    assert set(selected) == set(HEAD) | set(rest)
    fallback = _fallback_logs(caplog)
    assert [r.levelno for r in fallback] == [logging.WARNING]
    assert "filler_179" in fallback[0].getMessage()


async def test_same_cause_is_logged_once_then_at_debug(
    openai_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    session = FakeIndexSession(["filler_150"])
    session.fail = RuntimeError("no such table: tool_index")
    selector = _selector(FakeManager(session), openai_settings)
    schemas = _schemas(_registry_names())

    with caplog.at_level(logging.DEBUG, logger="pincer.tools.selection"):
        for _ in range(3):
            await selector.select(schemas, "x")
        session.fail = None
        await selector.select(schemas, "x")  # recovers
        session.fail = RuntimeError("no such table: tool_index")
        await selector.select(schemas, "x")  # fails again: a new episode, said again

    assert [r.levelno for r in _fallback_logs(caplog)] == [
        logging.WARNING,
        logging.DEBUG,
        logging.DEBUG,
        logging.WARNING,
    ]


async def test_disconnected_server_is_quiet_after_one_warning(
    openai_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """A configured but missing memory server: one warning, no per-turn sync tasks or warnings."""
    session = FakeIndexSession(["filler_150"])
    manager = FakeManager(session)
    manager.connected = False
    selector = _selector(manager, openai_settings)
    schemas = _schemas(_registry_names())
    selector.start_sync(schemas)
    await asyncio.sleep(0)

    with caplog.at_level(logging.INFO, logger="pincer.tools.selection"):
        for _ in range(3):
            selected = _names(await selector.select(schemas, "x"))
        await asyncio.sleep(0)

    assert len(selected) == 127
    assert session.calls == []
    assert selector._sync_task is not None and selector._sync_task.done()  # no new task per turn
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "not connected" in warnings[0]


async def test_ranking_starts_once_server_connects(openai_settings: Settings) -> None:
    """Whether the index is available is checked per turn, not fixed at startup."""
    session = FakeIndexSession(["filler_150"])
    manager = FakeManager(session)
    manager.connected = False
    selector = _selector(manager, openai_settings)
    schemas = _schemas(_registry_names())

    first = _names(await selector.select(schemas, "x"))
    manager.connected = True
    second = _names(await selector.select(schemas, "x"))

    assert "filler_150" not in first
    assert "filler_150" in second


async def test_old_server_names_dropped_tools_once_at_info(
    openai_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """An older memory server image (no tool_index_*): no index calls, no warnings, one info line."""
    session = FakeIndexSession(["filler_150"], has_index=False)
    selector = _selector(FakeManager(session), openai_settings)
    schemas = _schemas(_registry_names())
    selector.start_sync(schemas)

    with caplog.at_level(logging.INFO, logger="pincer.tools.selection"):
        await asyncio.sleep(0)
        selected = _names(await selector.select(schemas, "x"))
        await selector.select(schemas, "y")

    assert len(selected) == 127
    assert "make_phone_call" in selected
    assert session.calls == []
    assert [r.levelno for r in caplog.records] == [logging.INFO]
    assert "older image" in caplog.records[0].getMessage()
    assert "filler_179" in caplog.records[0].getMessage()


async def test_sync_still_running_falls_back_without_waiting(
    openai_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """A long first build (model download) must not hold each turn up to the timeout."""
    session = FakeIndexSession(["filler_150"])
    session.sync_gate = asyncio.Event()
    selector = _selector(FakeManager(session), openai_settings)
    schemas = _schemas(_registry_names())
    selector.start_sync(schemas)
    await asyncio.sleep(0)

    with caplog.at_level(logging.INFO, logger="pincer.tools.selection"):
        loop = asyncio.get_running_loop()
        started = loop.time()
        first = _names(await selector.select(schemas, "x"))
        assert loop.time() - started < 0.5  # well under the 3 s timeout

    assert "filler_150" not in first
    assert "still being built" in _fallback_logs(caplog)[0].getMessage()

    session.sync_gate.set()
    await asyncio.sleep(0)
    second = _names(await selector.select(schemas, "x"))
    assert "filler_150" in second
    assert session.count("tool_index_sync") == 1


async def test_empty_query_skips_ranking_quietly(openai_settings: Settings, caplog: pytest.LogCaptureFixture) -> None:
    session = FakeIndexSession(["filler_150"])
    with caplog.at_level(logging.INFO, logger="pincer.tools.selection"):
        selected = await _selector(FakeManager(session), openai_settings).select(_schemas(_registry_names()), "  ")
    assert len(selected) == 127
    assert session.count("tool_index_search") == 0
    assert not caplog.records


async def test_background_sync_failure_warns_once_per_cause(
    openai_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    session = FakeIndexSession(["filler_150"])
    session.fail = RuntimeError("Dimension mismatch for inserted vector")
    selector = _selector(FakeManager(session), openai_settings)
    schemas = _schemas(_registry_names())

    with caplog.at_level(logging.WARNING, logger="pincer.tools.selection"):
        for _ in range(2):
            selector.start_sync(schemas)
            await asyncio.sleep(0)

    sync_warnings = [r.getMessage() for r in caplog.records if "sync" in r.getMessage()]
    assert len(sync_warnings) == 1
    assert "Dimension mismatch" in sync_warnings[0]


def test_env_example_lists_the_default_tools(settings: Settings) -> None:
    """.env.example documents exactly the built-in tool_selection_default."""
    env_example = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
    line = next(x for x in env_example.splitlines() if x.startswith("# PINCER_TOOL_SELECTION_DEFAULT="))
    assert line.split("=", 1)[1].split(",") == settings.tool_selection_default


@pytest.mark.parametrize("enabled", [True, False])
async def test_serve_agent_gets_a_selector_without_mcp(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    """`pincer serve` builds its own agent (no MCP): default and recent tools still lead when it outgrows the cap."""
    from unittest.mock import MagicMock

    from pincer.api import _deps

    settings = settings.model_copy(update={"tool_selection_enabled": enabled})
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: settings)
    monkeypatch.setattr("pincer.llm.router.LLMRouter", MagicMock())

    agent = await _deps.build_agent_from_settings()
    try:
        assert isinstance(agent.tool_selector, ToolSelector) is enabled
        if enabled:
            assert agent.tool_selector._mcp_manager is None
    finally:
        await agent._sessions.close()
        await agent._costs.close()
