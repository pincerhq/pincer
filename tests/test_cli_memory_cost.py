"""Tests for `pincer memory` and `pincer cost` (typer.testing.CliRunner, real SQLite backends)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from pincer.cli import app

runner = CliRunner()


def _settings_for(tmp_path, **overrides: object) -> MagicMock:  # type: ignore[no-untyped-def]
    settings = MagicMock()
    settings.db_path = tmp_path / "pincer.db"
    settings.daily_budget_usd = 5.0
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def _seed_memories(db_path: object, entries: list[tuple[str, str, str]]) -> None:
    """entries: list of (user_id, content, category)."""
    from pincer.memory.sqlite import SQLiteMemoryBackend

    async def _seed() -> None:
        store = SQLiteMemoryBackend(db_path)  # type: ignore[arg-type]
        await store.initialize()
        for user_id, content, category in entries:
            await store.store_memory(user_id, content, category=category)
        await store.close()

    asyncio.run(_seed())


# ── memory ────────────────────────────────────────────────────────────────


def test_memory_search_no_results(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_for(tmp_path)
    _seed_memories(settings.db_path, [])
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: settings)

    result = runner.invoke(app, ["memory", "search", "radiology"])

    assert result.exit_code == 0
    assert "No memories found." in result.output


def test_memory_search_finds_match(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_for(tmp_path)
    _seed_memories(settings.db_path, [("user1", "discussed radiology project timelines", "general")])
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: settings)

    result = runner.invoke(app, ["memory", "search", "radiology"])

    assert result.exit_code == 0
    assert "radiology project" in result.output


def test_memory_stats(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_for(tmp_path)
    _seed_memories(
        settings.db_path,
        [
            ("user1", "first memory", "general"),
            ("user1", "second memory", "exchange"),
            ("user2", "third memory", "general"),
        ],
    )
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: settings)

    result = runner.invoke(app, ["memory", "stats"])

    assert result.exit_code == 0
    assert "Total memories: 3" in result.output
    assert "Users:          2" in result.output


def test_memory_clear(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_for(tmp_path)
    _seed_memories(settings.db_path, [("user1", "will be cleared", "general")])
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: settings)

    result = runner.invoke(app, ["memory", "clear", "--user", "user1"])

    assert result.exit_code == 0
    assert "Cleared memories for user1" in result.output


def test_memory_list_empty(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_for(tmp_path)
    _seed_memories(settings.db_path, [])
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: settings)

    result = runner.invoke(app, ["memory", "list"])

    assert result.exit_code == 0
    assert "No memories found." in result.output


def test_memory_list_with_filters(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_for(tmp_path)
    _seed_memories(
        settings.db_path,
        [
            ("user1", "first memory content", "general"),
            ("user1", "second memory content", "exchange"),
        ],
    )
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: settings)

    result = runner.invoke(app, ["memory", "list", "--user", "user1", "--limit", "5"])

    assert result.exit_code == 0
    assert "first memory content" in result.output
    assert "Showing 2 records" in result.output


def test_memory_export(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_for(tmp_path)
    _seed_memories(settings.db_path, [("user1", "exportable memory", "general")])
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: settings)

    output_path = tmp_path / "out.json"
    result = runner.invoke(app, ["memory", "export", "--user", "user1", "--output", str(output_path)])

    assert result.exit_code == 0
    assert "Exported 1 memories" in result.output
    assert output_path.exists()
    assert "exportable memory" in output_path.read_text()


# ── cost ──────────────────────────────────────────────────────────────────


def _seed_cost(db_path: object, daily_budget: float, entries: list[tuple[str, str, int, int]]) -> None:
    """entries: list of (provider, model, input_tokens, output_tokens)."""
    from pincer.llm.cost_tracker import CostTracker

    async def _seed() -> None:
        tracker = CostTracker(db_path, daily_budget)  # type: ignore[arg-type]
        await tracker.initialize()
        for provider, model, in_tok, out_tok in entries:
            await tracker.record(provider, model, in_tok, out_tok, is_free=True)
        await tracker.close()

    asyncio.run(_seed())


def test_cost_shows_zero_spend(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_for(tmp_path)
    _seed_cost(settings.db_path, settings.daily_budget_usd, [])
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: settings)

    result = runner.invoke(app, ["cost"])

    assert result.exit_code == 0
    assert "Pincer Cost Report" in result.output
    assert "Today:" in result.output


def test_cost_with_history_and_model_breakdown(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_for(tmp_path)
    _seed_cost(
        settings.db_path,
        settings.daily_budget_usd,
        [("anthropic", "claude-sonnet-4-5", 1000, 500)],
    )
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: settings)

    result = runner.invoke(app, ["cost", "--days", "7", "--by-model"])

    assert result.exit_code == 0
    assert "Last 7 days" in result.output
    assert "By Model" in result.output
    assert "claude-sonnet-4-5" in result.output


def test_cost_export(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = _settings_for(tmp_path)
    _seed_cost(settings.db_path, settings.daily_budget_usd, [("anthropic", "claude-sonnet-4-5", 100, 50)])
    monkeypatch.setattr("pincer.config.get_settings_relaxed", lambda: settings)

    export_path = tmp_path / "costs.json"
    result = runner.invoke(app, ["cost", "--export", str(export_path)])

    assert result.exit_code == 0
    assert "Exported to" in result.output
    assert export_path.exists()
