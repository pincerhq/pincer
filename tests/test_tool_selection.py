"""Tests for ToolSelector — per-turn tool budget with RAG ranking on the memory MCP server."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pincer.config import Settings
from pincer.tools.selection import ToolSelector

MEMORY_TOOLS = [f"sqlite_vec_memory__memory_{op}" for op in ("store", "search", "list", "get", "delete", "update")]


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
        return self.memory_tools if name == "sqlite_vec_memory" else []


def _schemas(names: list[str]) -> list[dict[str, Any]]:
    return [{"name": n, "description": f"{n} tool", "input_schema": {"type": "object"}} for n in names]


def _registry_names() -> list[str]:
    """189 tools with make_phone_call at index 141, like the production instance."""
    names = [f"filler_{i:03d}" for i in range(189 - 1 - len(MEMORY_TOOLS) - 2)]
    names = names[:141] + ["make_phone_call"] + names[141:]
    return names + ["send_file", "schedule_list"] + MEMORY_TOOLS


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(anthropic_api_key="sk-ant-test-key", data_dir=tmp_path / ".pincer")  # type: ignore[arg-type]


async def test_at_or_below_limit_returns_schemas_unchanged(settings: Settings) -> None:
    session = FakeIndexSession()
    selector = ToolSelector(FakeManager(session), settings)  # type: ignore[arg-type]
    schemas = _schemas([f"t{i}" for i in range(127)])
    assert await selector.select(schemas, "hello") is schemas
    assert session.calls == []


async def test_selection_order_and_budget(settings: Settings) -> None:
    names = _registry_names()
    ranking = ["filler_150", "make_phone_call", "filler_007", "slack__unknown"] + [
        f"filler_{i:03d}" for i in range(180)
    ]
    session = FakeIndexSession(ranking)
    selector = ToolSelector(FakeManager(session), settings)  # type: ignore[arg-type]

    selected = [s["name"] for s in await selector.select(_schemas(names), "Ruf mich an", ["filler_100"])]

    assert len(selected) == 127
    assert len(set(selected)) == 127
    head = ["make_phone_call", "send_file", "schedule_list", *MEMORY_TOOLS, "filler_100"]
    assert selected[: len(head)] == head
    assert selected[len(head) : len(head) + 2] == ["filler_150", "filler_007"]
    assert "slack__unknown" not in selected
    sync_args = next(args for name, args in session.calls if name == "tool_index_sync")
    assert {t["name"] for t in sync_args["tools"]} == set(names)
    search_args = next(args for name, args in session.calls if name == "tool_index_search")
    assert search_args["k"] == 127


async def test_default_list_override_replaces_head(settings: Settings) -> None:
    settings = settings.model_copy(update={"tool_selection_default": ["filler_00*"]})
    session = FakeIndexSession(["filler_150"])
    selector = ToolSelector(FakeManager(session, memory_tools=[]), settings)  # type: ignore[arg-type]

    selected = [s["name"] for s in await selector.select(_schemas(_registry_names()), "x")]

    assert selected[:11] == [f"filler_00{i}" for i in range(10)] + ["filler_150"]
    assert "make_phone_call" not in selected[:11]


def test_default_list_parses_comma_separated_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("PINCER_TOOL_SELECTION_DEFAULT", "send_file, schedule_*")
    s = Settings(anthropic_api_key="sk-ant-test-key", data_dir=tmp_path)  # type: ignore[arg-type]
    assert s.tool_selection_default == ["send_file", "schedule_*"]


async def test_index_synced_once_per_registry_version(settings: Settings) -> None:
    session = FakeIndexSession(["filler_150"])
    selector = ToolSelector(FakeManager(session), settings)  # type: ignore[arg-type]
    schemas = _schemas(_registry_names())

    await selector.select(schemas, "a")
    await selector.select(schemas, "b")
    assert session.count("tool_index_sync") == 1

    await selector.select([*schemas, *_schemas(["new_tool"])], "c")
    assert session.count("tool_index_sync") == 2


async def test_stale_search_resyncs_and_retries(settings: Settings) -> None:
    session = FakeIndexSession(["filler_150"])
    selector = ToolSelector(FakeManager(session), settings)  # type: ignore[arg-type]
    schemas = _schemas(_registry_names())
    await selector.select(schemas, "a")

    session.version = ""  # memory server restarted and lost its in-memory index
    selected = [s["name"] for s in await selector.select(schemas, "b")]

    assert session.count("tool_index_sync") == 2
    assert "filler_150" in selected[:20]


@pytest.mark.parametrize("failure", ["error", "timeout"])
async def test_server_failure_falls_back_to_head_then_registry_order(
    settings: Settings, failure: str, caplog: pytest.LogCaptureFixture
) -> None:
    session = FakeIndexSession(["filler_150"])
    if failure == "error":
        session.fail = ConnectionError("MCP 'sqlite_vec_memory' is not connected")
        selector = ToolSelector(FakeManager(session), settings)  # type: ignore[arg-type]
    else:
        session.search_delay = 1.0
        selector = ToolSelector(FakeManager(session), settings, timeout=0.05)  # type: ignore[arg-type]
    names = _registry_names()

    with caplog.at_level(logging.WARNING, logger="pincer.tools.selection"):
        selected = [s["name"] for s in await selector.select(_schemas(names), "Ruf mich an")]

    head = ["make_phone_call", "send_file", "schedule_list", *MEMORY_TOOLS]
    assert selected[: len(head)] == head
    assert selected[len(head) :] == [n for n in names if n not in head][: 127 - len(head)]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    fallback = [w for w in warnings if w.startswith("Tool selection fell back")]
    assert len(fallback) == 1
    assert "filler_179" in fallback[0]
    # The sync's own cause is logged once more (only for a failing sync, not a slow search).
    assert len(warnings) == (2 if failure == "error" else 1)


async def test_running_sync_gets_fallback_then_ranking(settings: Settings) -> None:
    session = FakeIndexSession(["filler_150"])
    session.sync_gate = asyncio.Event()
    selector = ToolSelector(FakeManager(session), settings, timeout=0.05)  # type: ignore[arg-type]
    schemas = _schemas(_registry_names())

    selector.start_sync(schemas)
    first = [s["name"] for s in await selector.select(schemas, "x")]
    assert "filler_150" not in first  # registry order: filler_150 sits past the budget

    session.sync_gate.set()
    await asyncio.sleep(0)
    second = [s["name"] for s in await selector.select(schemas, "x")]
    assert "filler_150" in second
    assert session.count("tool_index_sync") == 1


async def test_server_without_tool_index_skips_ranking_quietly(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """An older memory server image (no tool_index_*): no index calls, no warnings, one info line."""
    session = FakeIndexSession(["filler_150"], has_index=False)
    selector = ToolSelector(FakeManager(session), settings)  # type: ignore[arg-type]
    schemas = _schemas(_registry_names())
    selector.start_sync(schemas)

    with caplog.at_level(logging.INFO, logger="pincer.tools.selection"):
        await asyncio.sleep(0)
        selected = [s["name"] for s in await selector.select(schemas, "x")]
        await selector.select(schemas, "y")

    assert len(selected) == 127
    assert selected[0] == "make_phone_call"
    assert session.calls == []
    assert [r.levelno for r in caplog.records] == [logging.INFO]


async def test_ranking_starts_once_server_connects(settings: Settings) -> None:
    """Whether the index is available is checked per turn, not fixed at startup."""
    session = FakeIndexSession(["filler_150"])
    manager = FakeManager(session)
    manager.connected = False
    selector = ToolSelector(manager, settings)  # type: ignore[arg-type]
    schemas = _schemas(_registry_names())

    first = [s["name"] for s in await selector.select(schemas, "x")]
    manager.connected = True
    second = [s["name"] for s in await selector.select(schemas, "x")]

    assert "filler_150" not in first
    assert "filler_150" in second


async def test_background_sync_failure_warns_once_per_cause(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    session = FakeIndexSession(["filler_150"])
    session.fail = RuntimeError("Dimension mismatch for inserted vector")
    selector = ToolSelector(FakeManager(session), settings)  # type: ignore[arg-type]
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
