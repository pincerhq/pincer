# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project

Pincer (`pincer-agent`) is a self-hosted, security-first AI agent that operates across messaging channels (Telegram, WhatsApp, Discord, Slack, Signal, Email, Voice, Web) with 300+ native tools plus unlimited tools via MCP. Pure Python 3.12+ / `asyncio` — **no agent framework** (no LangChain, CrewAI, etc.), just provider SDKs (`anthropic`, `openai`) directly. The codebase is intentionally auditable. When building or extending LLM-facing functionality, prefer the latest Codex models and consult the `Codex-api` skill for current model IDs.

## Commands

Use `uv` (preferred). Tests run from the repo root; `pyproject.toml` sets `pythonpath = ["src"]` and `asyncio_mode = "auto"`.

```bash
uv sync --all-extras                 # install everything incl. dev + workspace MCP packages
uv run pytest                        # full suite
uv run pytest tests/test_agent.py    # one file
uv run pytest -k "test_approval" --no-cov   # by name filter
uv run pytest --cov=pincer --cov-report=term-missing

uv run ruff check .                  # lint
uv run ruff format .                 # format (CI fails on `ruff format --check .`)
uv run mypy src/                     # type check (strict)
uv run bandit -r src/pincer -ll -s B101,B104,B310,B608   # security scan

uv run pincer run                    # start agent (all configured channels)
uv run pincer doctor                 # 40+ config/security health checks
```

CI (`.github/workflows/ci.yml`) runs all of: pytest (incl. `src/ms365-mcp/tests/`), mypy on `src/`, `ruff check`, `ruff format --check`, and bandit. All must pass before merge.

Dashboard (React + Vite + TS in `dashboard/`, pnpm 10+): `cd dashboard && pnpm install && pnpm dev` (port 3000, proxies `/api` → `:8080`). CI runs `pnpm type-check`, `pnpm lint`, `pnpm test`.

## Architecture

**Agent loop** — `src/pincer/core/agent.py` is the ReAct (reason+act) brain: receive message → load session + memories → call LLM with available tools → if tool_call, execute (in sandbox, with approval) → feed result back → repeat until text → deliver → persist session/memory/cost. This single loop is shared across every channel.

**Channels** (`src/pincer/channels/`) — each subclasses `BaseChannel` (`base.py`); `router.py` (`ChannelRouter`) dispatches inbound messages into the agent and routes responses back to the originating channel. Channels are wired up centrally in `_run_agent()` in `src/pincer/cli.py` (~3000 LOC typer app). Every channel block follows the same sequence: **create → `set_identity_resolver` → start → append to channels/channel_map → `router.register`**. Note `ChannelType` in `base.py` and the channel-color map in `dashboard/src/lib/constants.ts` must also be updated for a new channel.

**LLM providers** (`src/pincer/llm/`) — `base.py` defines `BaseLLMProvider` and the shared message/tool/response types. Providers (anthropic, openai, grok, deepseek, gemini, groq, mistral, ollama) implement that interface; `cost_tracker.py` tracks spend. Automatic failover between configured providers is handled at the config layer (`config/main.py` `validate_api_keys` reassigns `default_provider` if the chosen one lacks a key).

**Tools** (`src/pincer/tools/`) — `registry.py` (`ToolRegistry`, with `register`/`deregister`) holds all callable tools; `sandbox.py` runs untrusted skills in subprocesses (memory/CPU/filesystem/network limits); `approval.py` gates destructive tools (`shell_exec`, `python_exec`, `file_write`, `email_send`, writes to external APIs) behind an in-chat ✅/❌ prompt. Built-ins live in `builtin/`. `@tool(name=..., description=..., requires_approval=...)` registers a function.

**Skills** (`src/pincer/tools/skills/` + bundled in top-level `skills/`) — dynamically loaded `skill.yaml` manifest + `main.py`; AST-scanned before install, optionally signed, sandboxed at runtime. Manifest `permissions` are enforced by the sandbox (no declared permission = denied).

**Config** (`src/pincer/config/`) — `Settings` in `config/main.py` composes mixin classes (`LLMSettings`, `ChannelSettings`, `ToolSettings`, `APISettings`, `CoreSettings`, `MCPSettings`) via pydantic-settings. **Env prefix is `PINCER_`**; all fields use `Field(default=...)`. `pincer.toml` provides `[[mcp.servers]]` and integration config.

**Memory** (`src/pincer/memory/`) — cross-channel SQLite store with FTS5 full-text + optional vector embeddings + auto-summarization. **Identity** (`src/pincer/core/identity.py`) maps users across channels; migrations add columns via the try/except "duplicate column" pattern.

**Integrations** (`src/pincer/integrations/`) — `google/`, `ms365/`, `slack/` provide large REST-backed tool sets registered in `_run_agent()` when configured/authenticated.

**MCP** (`src/pincer/mcp/`) — full client (`client.py`, `manager.py`, `bridge.py`) plus an OAuth 2.0 authorization server under `mcp/auth/`. Workspace MCP packages live in `src/pincer-mcps`, `src/pomodoro-mcp`, `src/sqlite-vec-memory-mcp`, `src/ms365-mcp` (declared as uv workspace members in `pyproject.toml`).

**Security** (`src/pincer/security/`) — `doctor.py` powers `pincer doctor`; to add a check, implement a `_check_*` method and register it in `run_all()`. Also `firewall.py`, `audit.py` (structured JSON log of every action), `rate_limiter.py`.

**Scheduler** (`src/pincer/scheduler/`) — `cron.py`, `proactive.py` (morning briefings), `triggers.py` (event-driven, e.g. Gmail pub/sub).

## Conventions

- Ruff: line length 120, target py312, rules `E,F,I,N,W,UP,B,SIM,TCH`.
- mypy is `strict`, but many modules are listed under `[[tool.mypy.overrides]]` in `pyproject.toml` with relaxed error codes — check there before fighting a type error in voice/channels/llm/tools modules.
- Async tests need no decorator (`asyncio_mode = "auto"`). Shared fixtures (`settings`, `mock_llm`, `session_manager`, `cost_tracker`, `tool_registry`, `sample_skill_dir`, `malicious_skill_dir`) are in `tests/conftest.py`.
- **Never add `__init__.py` to `tests/mcp/`** — it shadows the installed `mcp` package and breaks imports.
- Optional features are extras: `pip install "pincer-agent[mcp]"`, `[image]`, `[voice]`, `[slack]`, etc. (`[all]` for everything). Code must degrade gracefully when an extra isn't installed.
- Debug the agent loop with `PINCER_LOG_LEVEL=DEBUG pincer run`.
