# Pincer — Project Structure

> **Version:** 0.1.0 (Alpha)
> **Date:** February 21, 2026
> **License:** MIT

*Your personal AI agent. Text it on WhatsApp. It does stuff.*

---

## Overview

Pincer is a multi-channel personal AI assistant that implements a **ReAct (Reason + Act) loop**. Users can interact with it via Telegram (and eventually WhatsApp, Discord, Web). It connects to LLM providers (Anthropic, OpenAI), has a tool system for executing actions, persistent memory, session management, and cost tracking.

---

## Directory Tree

```
pincer/
├── .env.example                    # Environment variables template
├── .gitignore
├── .python-version                 # Python 3.12+
├── Dockerfile                      # Container image
├── docker-compose.yml              # Docker Compose setup
├── main.py                         # Application entry point
├── pyproject.toml                  # Project metadata & dependencies (hatchling)
├── uv.lock                         # Dependency lock file (uv)
├── README.md
│
├── .github/
│   └── workflows/
│       └── ci.yml                  # GitHub Actions CI (pytest, ruff)
│
├── docs/
│   └── PROJECT_STRUCTURE.md        # ← You are here
│
├── skills/                         # Custom agent skills (placeholder)
│
├── src/pincer/                     # Main Python package
│   ├── __init__.py
│   ├── __main__.py                 # `python -m pincer` entry
│   ├── cli.py                      # Typer CLI interface
│   ├── config.py                   # Pydantic Settings (env-based config)
│   ├── exceptions.py               # Custom exception classes
│   │
│   ├── core/                       # Agent brain
│   │   ├── __init__.py
│   │   ├── agent.py                # ReAct loop, streaming, tool execution
│   │   ├── events.py               # Event system (stub)
│   │   ├── identity.py             # Cross-channel identity resolver (Sprint 3)
│   │   └── session.py              # Session management (SQLite-backed)
│   │
│   ├── channels/                   # Communication channels
│   │   ├── __init__.py
│   │   ├── base.py                 # Abstract BaseChannel + ChannelType enum
│   │   ├── telegram.py             # Telegram bot (aiogram 3.x)
│   │   ├── whatsapp.py             # WhatsApp channel (neonize, Sprint 3)
│   │   ├── router.py               # Cross-channel message router (Sprint 3)
│   │   ├── discord_channel.py      # Discord (stub)
│   │   └── web.py                  # Web/HTTP channel (stub)
│   │
│   ├── llm/                        # LLM provider abstraction
│   │   ├── __init__.py
│   │   ├── base.py                 # Abstract BaseLLMProvider + message types
│   │   ├── anthropic_provider.py   # Anthropic Claude implementation
│   │   ├── openai_provider.py      # OpenAI GPT implementation
│   │   ├── ollama_provider.py      # Ollama local models (stub/partial)
│   │   └── cost_tracker.py         # Per-model cost tracking & budget limits
│   │
│   ├── memory/                     # Persistent memory system
│   │   ├── __init__.py
│   │   ├── store.py                # SQLite + FTS5 + vector similarity store
│   │   └── summarizer.py           # Conversation summarization
│   │
│   ├── tools/                      # Tool system
│   │   ├── __init__.py
│   │   ├── registry.py             # Tool registration, schema gen, dispatch
│   │   ├── approval.py             # Tool approval flow (stub)
│   │   ├── sandbox.py              # Tool sandboxing (stub)
│   │   │
│   │   └── builtin/                # Built-in tools
│   │       ├── __init__.py
│   │       ├── browser.py          # Playwright-based web browsing
│   │       ├── files.py            # File read/write/list operations
│   │       ├── python_exec.py      # Python code execution
│   │       ├── shell.py            # Shell command execution
│   │       ├── web_search.py       # DuckDuckGo / Tavily search
│   │       ├── transcribe.py       # Voice note transcription
│   │       ├── calendar_tool.py    # Google Calendar tools (Sprint 3)
│   │       └── email_tool.py       # IMAP/SMTP email tools (Sprint 3)
│   │
│   ├── security/                   # Security layer
│   │   ├── __init__.py
│   │   ├── audit.py                # Audit logging (stub)
│   │   ├── doctor.py               # Health checks (stub)
│   │   ├── firewall.py             # Firewall rules (stub)
│   │   └── rate_limiter.py         # Rate limiting (stub)
│   │
│   ├── scheduler/                  # Task scheduling (Sprint 3)
│   │   ├── __init__.py             # Package exports
│   │   ├── cron.py                 # CronScheduler — SQLite-backed cron loop
│   │   ├── proactive.py            # ProactiveAgent — morning briefing
│   │   └── triggers.py             # EventTriggerManager — email/calendar triggers
│   │
│   └── dashboard/                  # Admin dashboard
│       ├── __init__.py             # (stub)
│       └── templates/
│           └── .gitkeep
│
├── data/                           # Runtime data directory
│   └── migrations/
│       └── 003_sprint3.sql         # Sprint 3 DB schema migration
│
└── tests/                          # Test suite
    ├── conftest.py                 # Shared fixtures
    ├── test_agent.py               # Agent core tests
    ├── test_config.py              # Configuration tests
    ├── test_integration.py         # Integration tests
    ├── test_llm.py                 # LLM provider tests
    ├── test_telegram.py            # Telegram channel tests
    ├── test_tools.py               # Tool system tests
    ├── test_whatsapp.py            # WhatsApp channel tests (Sprint 3)
    ├── test_identity.py            # Cross-channel identity tests (Sprint 3)
    ├── test_email_tool.py          # Email tool tests (Sprint 3)
    ├── test_calendar_tool.py       # Calendar tool tests (Sprint 3)
    ├── test_scheduler.py           # Scheduler tests (Sprint 3)
    └── test_proactive.py           # Proactive agent tests (Sprint 3)
```

---

## Module Status

### Fully Implemented

| Module | Description |
|--------|-------------|
| `core/agent.py` | ReAct loop with tool execution, streaming, circuit breaker, budget enforcement |
| `core/session.py` | SQLite-backed session persistence with message history |
| `config.py` | Pydantic Settings with env vars, validation, API key auto-detection |
| `cli.py` | Typer CLI (`pincer run`, etc.) |
| `channels/base.py` | Abstract channel interface (`BaseChannel`, `IncomingMessage`) |
| `channels/telegram.py` | Full Telegram bot — text, voice, photos, documents, streaming, message splitting |
| `llm/base.py` | Abstract LLM interface, unified message types (`LLMMessage`, `ToolCall`, `ToolResult`) |
| `llm/anthropic_provider.py` | Anthropic Claude — complete + stream with tool use |
| `llm/openai_provider.py` | OpenAI GPT — complete + stream with tool use |
| `llm/cost_tracker.py` | Per-model token cost tracking with daily budget limits |
| `memory/store.py` | SQLite memory store with FTS5 full-text search and vector similarity |
| `memory/summarizer.py` | Conversation summarization to keep sessions manageable |
| `tools/registry.py` | Tool registration, auto-schema from type hints, execution dispatch |
| `tools/builtin/browser.py` | Playwright-based web browsing tool |
| `tools/builtin/files.py` | File read, write, list operations |
| `tools/builtin/python_exec.py` | Sandboxed Python code execution |
| `tools/builtin/shell.py` | Shell command execution (with approval support) |
| `tools/builtin/web_search.py` | Web search via DuckDuckGo / Tavily |
| `tools/builtin/transcribe.py` | Voice note transcription (OpenAI Whisper) |
| `exceptions.py` | Custom exceptions (`BudgetExceededError`, `LLMError`, `ToolNotFoundError`) |

### Sprint 3 — Implemented

| Module | Description |
|--------|-------------|
| `channels/whatsapp.py` | WhatsApp channel via neonize (QR pairing, text/voice/image/docs) |
| `channels/router.py` | Cross-channel message router for proactive delivery |
| `core/identity.py` | Unified cross-channel identity resolver |
| `tools/builtin/email_tool.py` | IMAP/SMTP email tools (check, send, search) |
| `tools/builtin/calendar_tool.py` | Google Calendar tools (today, week, create) |
| `scheduler/cron.py` | SQLite-backed cron scheduler with timezone support |
| `scheduler/proactive.py` | Morning briefing generator (weather, calendar, email, news) |
| `scheduler/triggers.py` | Event triggers (email notifications, calendar reminders, webhooks) |

### Stubs / Placeholders (Not Yet Implemented)

| Module | Description |
|--------|-------------|
| `channels/discord_channel.py` | Discord bot integration |
| `channels/web.py` | Web/HTTP REST channel |
| `tools/approval.py` | Human-in-the-loop tool approval workflow |
| `tools/sandbox.py` | Docker/subprocess sandboxing for tool execution |
| `security/audit.py` | Audit logging for all agent actions |
| `security/doctor.py` | Security health checks and diagnostics |
| `security/firewall.py` | Input/output filtering rules |
| `security/rate_limiter.py` | Per-user/channel rate limiting |
| `dashboard/` | Admin web dashboard with analytics |

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.12+ |
| Package Manager | uv (with hatchling build backend) |
| LLM Clients | `anthropic`, `openai` |
| HTTP | `httpx` |
| Telegram | `aiogram` 3.x |
| Database | `aiosqlite` (SQLite + FTS5) |
| Config | `pydantic-settings` |
| CLI | `typer` + `rich` |
| Logging | `structlog` |
| Browser | `playwright` (optional) |
| Search | `duckduckgo-search`, `tavily-python` (optional) |
| Testing | `pytest`, `pytest-asyncio`, `pytest-cov` |
| Linting | `ruff`, `mypy` |
| CI/CD | GitHub Actions |
| Containerization | Docker + Docker Compose |

---

## Key Architecture Patterns

- **ReAct Loop** — The agent reasons about what tool to use, acts (executes the tool), observes the result, and repeats until it has a final answer.
- **Provider Abstraction** — All LLM providers implement `BaseLLMProvider`, making it trivial to swap between Anthropic/OpenAI.
- **Channel Abstraction** — All communication channels implement `BaseChannel`, decoupling the agent from any specific messaging platform.
- **Tool Registry** — Tools are registered with JSON schemas auto-generated from Python type hints. The LLM picks tools, the registry dispatches.
- **Persistent Memory** — SQLite with FTS5 full-text search + optional vector similarity for long-term recall across sessions.
- **Cost Controls** — Per-model token pricing with daily budget limits to prevent runaway API costs.
- **Session Management** — Conversation history persisted in SQLite, with automatic summarization when sessions get long.
- **Streaming** — Token-by-token streaming to Telegram with live message editing.

---

## Entry Points

| Command | Description |
|---------|-------------|
| `python main.py` | Start the agent |
| `pincer run` | CLI entry (after install) |
| `python -m pincer` | Module entry |
| `docker compose up` | Run via Docker |

---

## Configuration

All configuration via environment variables with `PINCER_` prefix. See `.env.example` for the full list.

Key variables:
- `PINCER_ANTHROPIC_API_KEY` — Anthropic API key
- `PINCER_OPENAI_API_KEY` — OpenAI API key
- `PINCER_TELEGRAM_BOT_TOKEN` — Telegram bot token
- `PINCER_DEFAULT_PROVIDER` — `anthropic` or `openai`
- `PINCER_DAILY_BUDGET_USD` — Daily spend limit (default: $5.00)
- `PINCER_MEMORY_ENABLED` — Enable persistent memory (default: true)
