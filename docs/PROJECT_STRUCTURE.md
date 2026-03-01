# Pincer — Project Structure

> **Version:** 0.3.0 (Sprint 3 Complete)
> **Date:** February 23, 2026
> **License:** MIT

*Your personal AI agent. Text it on Telegram or WhatsApp. It does stuff.*

---

## Sprint History

| Sprint | Dates | Theme | Key Deliverables |
|--------|-------|-------|-----------------|
| 1 | Days 1–7 | Foundation | ReAct agent core, Telegram bot, tool system, session persistence, cost tracking |
| 2 | Days 8–14 | Memory & Polish | Persistent memory (SQLite + FTS5), conversation summarizer, browser tool, voice transcription, streaming responses, send_image tool |
| 3 | Days 15–21 | WhatsApp + Proactive | WhatsApp channel (neonize), cross-channel identity, email & calendar tools, cron scheduler, morning briefings, event triggers |

---

## Sprint 3 — What Was Built

### WhatsApp Channel (`channels/whatsapp.py`)
- Full WhatsApp integration via **neonize** (whatsmeow Go backend)
- QR code pairing displayed in terminal on first run
- Self-chat mode: message yourself → Pincer responds (LID-aware detection)
- DM allowlist: only approved phone numbers can message the bot
- Group chat: responds when @mentioned or trigger word used
- Supports text, voice notes (auto-transcribed), images, and documents
- History-sync filter: ignores old messages on reconnect
- Monkey-patched Go callback error handling for debugging
- Comprehensive diagnostic logging on every message routing decision

### Cross-Channel Identity (`core/identity.py`)
- Unified `pincer_user_id` across Telegram and WhatsApp
- Deterministic hash-based ID generation from channel-specific identifiers
- SQLite-backed `identity_map` table
- Config-driven identity seeding at startup via `PINCER_IDENTITY_MAP`
- Enables proactive messages to reach users on any connected channel

### Channel Router (`channels/router.py`)
- Routes proactive/scheduled messages to the correct channel for each user
- Looks up all channels registered for a given `pincer_user_id`
- Used by scheduler, triggers, and briefing system

### Email Tools (`tools/builtin/email_tool.py`)
- `email_check` — Check inbox for unread emails (IMAP)
- `email_send` — Send emails (SMTP)
- `email_search` — Search emails by query
- Gmail App Password support (required since Google disabled plain passwords)
- Credential validation before connection attempts

### Google Calendar Tools (`tools/builtin/calendar_tool.py`)
- `calendar_today` — List today's events
- `calendar_week` — List this week's events
- `calendar_create` — Create new calendar events
- OAuth2 flow with automatic token refresh
- Dedicated `pincer auth-google` CLI command for one-time consent
- Actionable error messages for missing credentials

### Cron Scheduler (`scheduler/cron.py`)
- SQLite-backed cron job persistence
- Timezone-aware scheduling via `croniter`
- 60-second tick interval with missed-job detection

### Proactive Agent (`scheduler/proactive.py`)
- Morning briefing: weather (OpenWeatherMap) + calendar + email + news (NewsAPI)
- Customizable via `briefing_config` table
- Delivered to user's preferred channel via router

### Event Triggers (`scheduler/triggers.py`)
- Email polling trigger (new unread emails)
- Calendar reminder trigger (upcoming events)
- Webhook receiver (HTTP POST)
- Deduplication via `event_triggers` table

### Agent Hardening (`core/agent.py`)
- `_sanitize_tool_pairs` — fixes orphaned tool_use/tool_result messages before LLM calls
- Proactive sanitization before every LLM request
- Retry limit with session-clear fallback for persistent API errors
- Circuit breaker for consecutive tool failures
- Anthropic provider-level message validation (`_validate_api_messages`)

### Session Hardening (`core/session.py`, `memory/summarizer.py`)
- Trim logic prevents orphaned tool_use at session boundaries
- Summarizer split-point logic keeps tool_use/tool_result pairs together

### Database Migration (`data/migrations/003_sprint3.sql`)
- `identity_map` — cross-channel user identity
- `schedules` — cron job persistence
- `briefing_config` — per-user briefing preferences
- `event_triggers` — trigger deduplication
- `sessions.pincer_user_id` column backfill

---

## Current System Architecture

```
┌─────────────────────────────────────────────────────────┐
│                        User                              │
│              Telegram  ·  WhatsApp  ·  (future)          │
└────────┬──────────────────┬──────────────────────────────┘
         │                  │
   ┌─────▼─────┐    ┌──────▼──────┐
   │ Telegram   │    │  WhatsApp   │
   │ Channel    │    │  Channel    │
   │ (aiogram)  │    │  (neonize)  │
   └─────┬──────┘    └──────┬──────┘
         │                  │
   ┌─────▼──────────────────▼──────┐
   │       Identity Resolver        │
   │   (cross-channel user map)     │
   └─────────────┬─────────────────┘
                 │
   ┌─────────────▼─────────────────┐
   │         Agent Core             │
   │  (ReAct loop · streaming ·     │
   │   tool execution · sessions)   │
   └──┬──────────┬──────────┬──────┘
      │          │          │
 ┌────▼───┐ ┌───▼────┐ ┌───▼──────────┐
 │  LLM   │ │ Memory │ │    Tools      │
 │Provider │ │ Store  │ │  (15 built-in)│
 │Anthropic│ │ SQLite │ │              │
 │ OpenAI  │ │ + FTS5 │ │ web_search   │
 └────────┘ └────────┘ │ shell_exec   │
                        │ file_*       │
                        │ browse       │
                        │ screenshot   │
                        │ python_exec  │
                        │ email_*      │
                        │ calendar_*   │
                        │ send_file    │
                        │ send_image   │
                        │ transcribe   │
                        └──────────────┘

   ┌────────────────────────────────┐
   │        Scheduler               │
   │  Cron · Triggers · Briefings   │
   │         ↓                      │
   │    Channel Router              │
   │  (proactive message delivery)  │
   └────────────────────────────────┘
```

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
├── data/
│   ├── google_credentials.json     # Google OAuth client (gitignored)
│   └── migrations/
│       └── 003_sprint3.sql         # Sprint 3 DB schema migration
│
├── src/pincer/                     # Main Python package (~10,600 lines)
│   ├── __init__.py
│   ├── __main__.py                 # `python -m pincer` entry
│   ├── cli.py                      # Typer CLI (run, config, cost, auth-google, pair-whatsapp)
│   ├── config.py                   # Pydantic Settings (env-based config)
│   ├── exceptions.py               # Custom exception classes
│   │
│   ├── core/                       # Agent brain
│   │   ├── agent.py                # ReAct loop, streaming, tool execution, sanitization
│   │   ├── events.py               # Event system (stub)
│   │   ├── identity.py             # Cross-channel identity resolver + config seeding
│   │   └── session.py              # Session management (SQLite-backed, trim-safe)
│   │
│   ├── channels/                   # Communication channels
│   │   ├── base.py                 # Abstract BaseChannel, IncomingMessage, ChannelType
│   │   ├── telegram.py             # Telegram bot (aiogram 3.x, streaming)
│   │   ├── whatsapp.py             # WhatsApp (neonize/whatsmeow, LID-aware)
│   │   ├── router.py               # Cross-channel message router
│   │   ├── discord_channel.py      # Discord (stub)
│   │   └── web.py                  # Web/HTTP channel (stub)
│   │
│   ├── llm/                        # LLM provider abstraction
│   │   ├── base.py                 # Abstract BaseLLMProvider + message types
│   │   ├── anthropic_provider.py   # Anthropic Claude (with message validation)
│   │   ├── openai_provider.py      # OpenAI GPT implementation
│   │   ├── ollama_provider.py      # Ollama local models (stub)
│   │   └── cost_tracker.py         # Per-model cost tracking & budget limits
│   │
│   ├── memory/                     # Persistent memory system
│   │   ├── store.py                # SQLite + FTS5 + vector similarity
│   │   └── summarizer.py           # Conversation summarization (pair-safe)
│   │
│   ├── tools/                      # Tool system (15 built-in tools)
│   │   ├── registry.py             # Registration, schema gen, dispatch
│   │   ├── approval.py             # Tool approval flow (stub)
│   │   ├── sandbox.py              # Tool sandboxing (stub)
│   │   └── builtin/
│   │       ├── browser.py          # Playwright-based web browsing
│   │       ├── calendar_tool.py    # Google Calendar (OAuth2, today/week/create)
│   │       ├── email_tool.py       # IMAP/SMTP (check/send/search)
│   │       ├── files.py            # File read/write/list
│   │       ├── python_exec.py      # Python code execution
│   │       ├── shell.py            # Shell command execution
│   │       ├── transcribe.py       # Voice transcription (OpenAI Whisper)
│   │       └── web_search.py       # DuckDuckGo / Tavily search
│   │
│   ├── scheduler/                  # Task scheduling & proactive agent
│   │   ├── cron.py                 # SQLite-backed cron scheduler
│   │   ├── proactive.py            # Morning briefing generator
│   │   └── triggers.py             # Email/calendar/webhook triggers
│   │
│   ├── security/                   # Security layer (stubs)
│   │   ├── audit.py
│   │   ├── doctor.py
│   │   ├── firewall.py
│   │   └── rate_limiter.py
│   │
│   └── dashboard/                  # Admin dashboard (stub)
│       └── __init__.py
│
└── tests/                          # Test suite (~1,480 lines)
    ├── conftest.py                 # Shared fixtures
    ├── test_agent.py               # Agent core tests
    ├── test_calendar_tool.py       # Calendar tool tests
    ├── test_config.py              # Configuration tests
    ├── test_email_tool.py          # Email tool tests
    ├── test_identity.py            # Cross-channel identity tests
    ├── test_integration.py         # Integration tests
    ├── test_llm.py                 # LLM provider tests
    ├── test_proactive.py           # Proactive agent tests
    ├── test_scheduler.py           # Scheduler tests
    ├── test_telegram.py            # Telegram channel tests
    ├── test_tools.py               # Tool system tests
    └── test_whatsapp.py            # WhatsApp channel tests
```

---

## Module Status

### Fully Implemented & Production-Tested

| Module | Description |
|--------|-------------|
| `core/agent.py` | ReAct loop with tool execution, streaming, circuit breaker, budget enforcement, session sanitization |
| `core/session.py` | SQLite-backed session persistence with trim-safe message history |
| `core/identity.py` | Cross-channel identity resolver with config-driven seeding |
| `config.py` | Pydantic Settings with env vars, validation, API key auto-detection |
| `cli.py` | Typer CLI (`run`, `config`, `cost`, `auth-google`, `pair-whatsapp`) |
| `channels/base.py` | Abstract channel interface (`BaseChannel`, `IncomingMessage`, `ChannelType`) |
| `channels/telegram.py` | Full Telegram bot — text, voice, photos, documents, streaming, message splitting |
| `channels/whatsapp.py` | WhatsApp via neonize — QR pairing, self-chat (LID-aware), DM allowlist, groups, voice/image/docs |
| `channels/router.py` | Cross-channel message router for proactive delivery |
| `llm/base.py` | Abstract LLM interface, unified message types |
| `llm/anthropic_provider.py` | Anthropic Claude — complete + stream with tool use, message validation |
| `llm/openai_provider.py` | OpenAI GPT — complete + stream with tool use |
| `llm/cost_tracker.py` | Per-model token cost tracking with daily budget limits |
| `memory/store.py` | SQLite memory store with FTS5 full-text search |
| `memory/summarizer.py` | Conversation summarization with pair-safe splitting |
| `tools/registry.py` | Tool registration, auto-schema from type hints, execution dispatch |
| `tools/builtin/browser.py` | Playwright-based web browsing |
| `tools/builtin/calendar_tool.py` | Google Calendar — OAuth2, today/week/create |
| `tools/builtin/email_tool.py` | IMAP/SMTP — check/send/search with credential validation |
| `tools/builtin/files.py` | File read, write, list operations |
| `tools/builtin/python_exec.py` | Sandboxed Python code execution |
| `tools/builtin/shell.py` | Shell command execution |
| `tools/builtin/transcribe.py` | Voice note transcription (OpenAI Whisper) |
| `tools/builtin/web_search.py` | Web search via DuckDuckGo / Tavily |
| `scheduler/cron.py` | SQLite-backed cron scheduler with timezone support |
| `scheduler/proactive.py` | Morning briefing (weather, calendar, email, news) |
| `scheduler/triggers.py` | Event triggers (email polling, calendar reminders, webhooks) |
| `exceptions.py` | Custom exceptions (`BudgetExceededError`, `LLMError`, `ToolNotFoundError`, `ChannelNotConnectedError`) |

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
| `llm/ollama_provider.py` | Ollama local model provider |

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.12+ |
| Package Manager | uv (with hatchling build backend) |
| LLM Clients | `anthropic`, `openai` |
| HTTP | `httpx` |
| Telegram | `aiogram` 3.x |
| WhatsApp | `neonize` 0.3.14 (whatsmeow Go backend) |
| Database | `aiosqlite` (SQLite + FTS5) |
| Config | `pydantic-settings` |
| CLI | `typer` + `rich` |
| Logging | `structlog` |
| Browser | `playwright` (optional) |
| Search | `duckduckgo-search`, `tavily-python` (optional) |
| Email | `aioimaplib`, `aiosmtplib` |
| Calendar | `google-api-python-client`, `google-auth-oauthlib` |
| Scheduling | `croniter` |
| QR Display | `qrcode[pil]` |
| Testing | `pytest`, `pytest-asyncio`, `pytest-cov` |
| Linting | `ruff`, `mypy` |
| CI/CD | GitHub Actions |
| Containerization | Docker + Docker Compose |

---

## Key Architecture Patterns

- **ReAct Loop** — The agent reasons about what tool to use, acts (executes the tool), observes the result, and repeats until it has a final answer.
- **Provider Abstraction** — All LLM providers implement `BaseLLMProvider`, making it trivial to swap between Anthropic/OpenAI.
- **Channel Abstraction** — All communication channels implement `BaseChannel`, decoupling the agent from any specific messaging platform.
- **Cross-Channel Identity** — A unified `pincer_user_id` maps Telegram user IDs and WhatsApp phone numbers to a single identity, enabling seamless cross-channel experiences.
- **Tool Registry** — Tools are registered with JSON schemas auto-generated from Python type hints. The LLM picks tools, the registry dispatches.
- **Persistent Memory** — SQLite with FTS5 full-text search for long-term recall across sessions.
- **Cost Controls** — Per-model token pricing with daily budget limits to prevent runaway API costs.
- **Session Management** — Conversation history persisted in SQLite, with automatic summarization and pair-safe trimming.
- **Streaming** — Token-by-token streaming to Telegram with live message editing.
- **Proactive Agent** — Scheduled tasks (morning briefings, email alerts, calendar reminders) delivered via channel router without user prompting.
- **Session Sanitization** — Automatic repair of corrupted message histories (orphaned tool_use/tool_result pairs) before every LLM call.

---

## Entry Points

| Command | Description |
|---------|-------------|
| `pincer run` | Start the agent (all channels + scheduler) |
| `pincer config` | Show current configuration |
| `pincer cost` | Show today's spend |
| `pincer auth-google` | Run Google Calendar OAuth consent flow |
| `pincer pair-whatsapp` | Pair WhatsApp via QR code |
| `python main.py` | Start the agent (alternative) |
| `python -m pincer` | Module entry |
| `docker compose up` | Run via Docker |

---

## Configuration

All configuration via environment variables with `PINCER_` prefix. See `.env.example` for the full list.

### Required Keys

| Variable | Purpose |
|----------|---------|
| `PINCER_ANTHROPIC_API_KEY` | Anthropic Claude API key |
| `PINCER_OPENAI_API_KEY` | OpenAI API key (also used for voice transcription) |
| `PINCER_TELEGRAM_BOT_TOKEN` | Telegram bot token (from @BotFather) |

### Optional — WhatsApp

| Variable | Purpose |
|----------|---------|
| `PINCER_WHATSAPP_ENABLED` | Enable WhatsApp channel (`true`/`false`) |
| `PINCER_WHATSAPP_DM_ALLOWLIST` | Comma-separated phone numbers allowed to DM |
| `PINCER_WHATSAPP_GROUP_TRIGGER` | Trigger word for group messages (default: `pincer`) |

### Optional — Email & Calendar

| Variable | Purpose |
|----------|---------|
| `PINCER_EMAIL_USERNAME` | Gmail address |
| `PINCER_EMAIL_PASSWORD` | Gmail App Password (not regular password) |
| `PINCER_IDENTITY_MAP` | Cross-channel identity mapping |
| `PINCER_OPENWEATHERMAP_API_KEY` | Weather for morning briefings |
| `PINCER_NEWSAPI_KEY` | News for morning briefings |

### First-Time Setup

1. `cp .env.example .env` — fill in API keys
2. `pincer auth-google` — one-time Google Calendar consent
3. `pincer run` — scan WhatsApp QR on first launch
4. Message yourself on WhatsApp or talk to the bot on Telegram

---

## Codebase Stats

| Metric | Value |
|--------|-------|
| Source code | ~10,600 lines |
| Test code | ~1,480 lines |
| Total Python files | 42 |
| Test files | 13 |
| Built-in tools | 15 |
| Active channels | 2 (Telegram, WhatsApp) |
| Commits (sprint3) | 9 |
