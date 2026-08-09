# Pincer — Project Structure

> **Date:** March 26, 2026
> **License:** MIT

*Your personal AI agent. Text it on Telegram or WhatsApp. It does stuff.*

---

## Sprint History

| Sprint | Dates | Theme | Key Deliverables |
|--------|-------|-------|-----------------|
| 1 | Days 1–7 | Foundation | ReAct agent core, Telegram bot, tool system, session persistence, cost tracking |
| 2 | Days 8–14 | Memory & Polish | Persistent memory (SQLite + FTS5), conversation summarizer, browser tool, voice transcription, streaming responses, send_image tool |
| 3 | Days 15–21 | WhatsApp + Proactive | WhatsApp channel (neonize), cross-channel identity, email & calendar tools, cron scheduler, morning briefings, event triggers |
| 4–5 | — | Dashboard + Security | FastAPI dashboard, audit log, security doctor, OAuth 2.1, rate limiting |
| 6 | — | Discord + Web | Discord channel, Web/HTTP channel, multi-channel router |
| 7 | — | Voice Calling | Twilio Voice, real-time STT/TTS, call state machine, barge-in |
| 7.5 | — | Signal Channel | Signal via signal-cli-rest-api sidecar, WebSocket receive mode |
| 8 | — | Image Generation + Grok | fal.ai + Gemini image generation, Grok/xAI LLM provider |
| A0 | Mar 21, 2026 | MCP Architecture | MCPServiceCore, layered shells (embedded + standalone), resources, prompts, sampling, `pincer mcp serve` |
| 9 | Mar 26, 2026 | Google Workspace | 85-tool native integration (Gmail 19, Calendar 12, Drive 15, Docs 8, Sheets 10, Slides 6, Tasks 8, Contacts 7), `pincer setup-google` |
| 9.5 | — | Google Meet | Extended Google Workspace integration to 113 tools — full Meet v2 REST surface (27 tools: spaces, conference records, participants, recordings, transcripts, smart notes, event subscriptions) |
| 10 | Apr 6, 2026 | Microsoft 365 | 69-tool standalone MCP server (Outlook 17, Calendar 12, OneDrive 14, To Do 8, Teams 7, Contacts 6, OneNote 5), `ms365-mcp-setup` |
| 11 | Apr 2026 | Slack Native | 71-tool native integration (messages 18, channels 16, users 10, files 10, misc 12, reactions 5) on top of the existing Slack channel; supports bot + user tokens for full-text search |
| 12 | Apr 2026 | MCP OAuth 2.0 | Full OAuth 2.0 Authorization Server for MCP — `/authorize`, `/token`, `/introspect`, `/revoke`, RFC 8414 metadata, PKCE, JWT tokens, scope enforcement, `token_store.py` (SQLite + keyring) |
| 13 | Apr 17, 2026 | Tools Catalog | 304 first-party tools catalogued in `docs/TOOLS_CATALOG.md`; README + docs refreshed to reflect 600+ with popular MCP servers |
| 14 | Aug 9, 2026 | Background Task Execution | Hand-rolled cron fire-loop replaced with durable [repid](https://pypi.org/project/repid/) actors (`tasks/`); chat-driven `schedule_create/list/remove/toggle` tools; session-free `Agent.run_headless`; `Deliverer`/`ResultRelay` pub-sub bridge; standalone `pincer run tasks` worker + Redis broker for horizontal scaling |

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
- Unified `pincer_user_id` across all channels — tell the agent something on WhatsApp, ask about it on Telegram, it remembers
- Two-table normalized schema: `identity_meta` (one row per user) + `channel_identities` (many-to-many channel links)
- Config-driven identity seeding at startup via `PINCER_IDENTITY_MAP`; supports N channels per identity: `name@ch1:id1=ch2:id2=ch3:id3`
- Named canonical IDs (`john@telegram:...`) make memory tags human-readable and portable across DB rebuilds
- Hash-based fallback ID (`usr_abc123...`) auto-generated when no name is configured — backward-compatible
- Per-channel ID resolution: phone numbers, email addresses, and @usernames in config are resolved to the channel's real internal ID by each channel driver before being stored (`resolve_internal_user_id()` on `BaseChannel`)
- Automatic conflict merging when multiple identities are unified by a new config entry
- Legacy `identity_map` table automatically migrated to the new schema on first boot

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
- **Strict response validation:** Success only when API returns both `id` and `htmlLink`; otherwise returns error (no silent failure)
- **IANA timezone handling:** Naive datetimes use `settings.timezone`; fixed offsets fall back to IANA; ZoneInfo preserved
- **Success message:** Always includes `htmlLink` for user verification; includes `calendar_id` when non-primary
- **Agent instructions:** System prompt and tool description require agent to include event link in final response and pass errors through

### Cron Scheduler (`scheduler/cron.py`)
- SQLite-backed cron job persistence
- Timezone-aware scheduling via `croniter`
- As of Sprint 14, this module is store-only — polling and execution moved to
  the `tasks/` package (durable [repid](https://pypi.org/project/repid/)
  actors); see [Background Tasks](../core-components/background-tasks.md)

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

## Sprint A0 — MCP Architecture Overhaul

### Layered MCP design

```
┌─────────────────────────────────────┐
│         MCPServiceCore              │  agent-independent: tools, resources,
│  (tools · resources · prompts ·     │  prompts, sampling, security, audit
│   sampling · security · audit)      │  ~230 LOC
└──────────┬──────────────┬───────────┘
           │              │
  ┌────────▼──────┐  ┌────▼──────────────┐
  │ EmbeddedShell │  │  StandaloneShell  │
  │  ~175 LOC     │  │  ~175 LOC         │
  │               │  │                   │
  │ pincer run    │  │ pincer mcp serve  │
  │               │  │                   │
  │ Wires to:     │  │ No agent, no      │
  │ - agent loop  │  │ channels.         │
  │ - channels    │  │ Approval via:     │
  │ - ask_user    │  │ - policy (auto)   │
  │ - LLM router  │  │ - cli (terminal)  │
  │ - memory      │  │ - webhook (HTTP)  │
  └───────────────┘  └───────────────────┘
```

### New modules (`src/pincer/mcp/`)

| File | Purpose |
|------|---------|
| `core.py` | `MCPServiceCore` — agent-independent coordinator |
| `approval.py` | `ApprovalBackend` + 4 implementations |
| `resources.py` | `ResourceRegistry` — MCP resources/\* protocol |
| `prompts.py` | `PromptRegistry` — MCP prompts/\* protocol |
| `sampling.py` | `SamplingHandler` + `LLMBackend` — MCP sampling/\* protocol |
| `embedded.py` | `EmbeddedMCPShell` — wires core to running agent |
| `standalone.py` | `StandaloneMCPShell` — headless MCP server |

### ApprovalBackend implementations

| Class | Approval mechanism | Mode |
|-------|--------------------|------|
| `ChannelApprovalBackend` | Routes to Telegram/WhatsApp/Signal | Embedded |
| `PolicyApprovalBackend` | Auto-approve/deny from config rules | Standalone headless |
| `CLIApprovalBackend` | Interactive terminal `y/N` prompt | Standalone interactive |
| `WebhookApprovalBackend` | POST to external approval service | Both modes |

### MCP spec compliance after Sprint A0

| Feature | Spec section | Status |
|---------|-------------|--------|
| Tools | tools/\* | ✅ Full |
| Resources | resources/\* | ✅ Full |
| Prompts | prompts/\* | ✅ Full |
| Sampling | sampling/\* | ✅ Infrastructure + budget |
| Auth (OAuth 2.1) | authorization/\* | ✅ Full |
| Streamable HTTP | transports | ✅ Full |
| stdio | transports | ✅ Client + server |
| Notifications | notifications/\* | ✅ tools/list\_changed |
| Logging | logging/\* | ✅ Audit log |

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
 ┌────▼───┐ ┌───▼────┐ ┌───▼────────────────┐
 │  LLM   │ │ Memory │ │   Tools (304 native)│
 │Provider │ │ Store  │ │                     │
 │Anthropic│ │ SQLite │ │ 24  core built-ins  │
 │ OpenAI  │ │ + FTS5 │ │ 27  bundled skills  │
 │ Grok    │ │        │ │ 113 Google Workspace│
 │ Ollama  │ │        │ │ 69  Microsoft 365   │
 └────────┘ └────────┘ │ 71  Slack native    │
                       │  +  MCP (unlimited) │
                       └─────────────────────┘

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
│   │   ├── anthropic_common.py     # Anthropic-wire provider (well-known + compatible)
│   │   ├── openai_common.py        # OpenAI-wire provider (well-known + compatible)
│   │   ├── router.py               # LLMRouter — construction + random failover entry point
│   │   └── cost_tracker.py         # Per-model cost tracking & budget limits
│   │
│   ├── memory/                     # Persistent memory system
│   │   ├── store.py                # SQLite + FTS5 + vector similarity
│   │   └── summarizer.py           # Conversation summarization (pair-safe)
│   │
│   ├── tools/                      # Tool system (24 core built-ins + skills loader; see docs/TOOLS_CATALOG.md)
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
│   │       └── transcribe.py       # Voice transcription (OpenAI Whisper)
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
│   │   ├── __init__.py             # Public exports
│   │   ├── config.py               # MCPConfig, MCPServerConfig, load_mcp_config
│   │   ├── client.py               # MCPClientSession (stdio + http)
│   │   ├── manager.py              # MCPClientManager (lifecycle + health loop)
│   │   ├── bridge.py               # MCP tools → Pincer ToolRegistry bridge
│   │   └── audit.py                # MCPAuditLogger
│   │
│   ├── integrations/               # First-party service integrations
│   │   └── google/                 # Google Workspace (113 tools incl. Meet v2)
│   │       ├── __init__.py         # get_google_factory(), register_all_tools() → 85
│   │       ├── auth.py             # GoogleAuth (InstalledAppFlow, token cache)
│   │       ├── service_factory.py  # GoogleServiceFactory (30-min service cache)
│   │       ├── quota.py            # with_backoff() (429/503/rateLimitExceeded)
│   │       ├── pagination.py       # collect_pages() (nextPageToken)
│   │       ├── models.py           # fmt_* formatting helpers
│   │       ├── tools_gmail.py      # 19 Gmail tools
│   │       ├── tools_calendar.py   # 12 Calendar tools
│   │       ├── tools_drive.py      # 15 Drive tools
│   │       ├── tools_docs.py       # 8 Docs tools
│   │       ├── tools_sheets.py     # 10 Sheets tools
│   │       ├── tools_slides.py     # 6 Slides tools
│   │       ├── tools_tasks.py      # 8 Tasks tools
│   │       ├── tools_contacts.py   # 7 Contacts/People API tools
│   │       └── server.py           # Standalone FastMCP server (stdio or HTTP port 18900)
│   │
│   └── dashboard/                  # Admin dashboard (stub)
│       └── __init__.py
│
└── tests/                          # Test suite (~3,500 lines)
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
    ├── test_whatsapp.py            # WhatsApp channel tests
    └── integrations/
        └── google/                 # Google Workspace tests (131 tests, all mocked)
            ├── conftest.py         # Mock fixtures (mock_factory, per-service mocks)
            ├── test_auth.py        # GoogleAuth unit tests (10)
            ├── test_tools_gmail.py # Gmail tool tests (19)
            ├── test_tools_calendar.py # Calendar tool tests (14)
            ├── test_tools_drive.py # Drive tool tests (17)
            ├── test_tools_sheets.py # Sheets tool tests (14)
            ├── test_tools_docs.py  # Docs tool tests (10)
            ├── test_tools_other.py # Slides + Tasks + Contacts tests (21)
            └── test_server.py      # Registration + factory tests (9)
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
| `llm/anthropic_common.py` | Anthropic-wire provider — complete + stream with tool use, message validation |
| `llm/openai_common.py` | OpenAI-wire provider — complete + stream with tool use |
| `llm/router.py` | LLMRouter — single construction entry point + random 1+N failover |
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
| `scheduler/cron.py` | SQLite-backed cron schedule store with timezone support (polling/execution live in `tasks/`) |
| `scheduler/proactive.py` | Morning briefing (weather, calendar, email, news) |
| `scheduler/triggers.py` | Event triggers (email polling, calendar reminders, webhooks) |
| `tasks/app.py` | repid app — broker registration (in-memory or Redis), actor router |
| `tasks/dispatch.py` | `ScheduleDispatcher` — polls due cron schedules, enqueues repid messages |
| `tasks/actors.py` | repid actors — `run_scheduled_action`, `process_webhook`, retried via tenacity |
| `tasks/context.py` | Process-global deliverer/proactive/triggers for actor bodies |
| `tasks/delivery.py` | `Deliverer`/`ResultEmitter`/`ResultRelay` — pub-sub result delivery bridge for split deployments |
| `tools/builtin/schedule_tool.py` | Chat-driven `schedule_create/list/remove/toggle` tools |
| `exceptions.py` | Custom exceptions (`BudgetExceededError`, `LLMError`, `ToolNotFoundError`, `ChannelNotConnectedError`) |

| `mcp/` | MCP platform — client (consume external MCP servers) + server (expose Pincer tools via MCP); sandboxed stdio subprocesses; streamable-http transport; OAuth 2.1; audit trail; registry client (MCP Registry + ClawHub); 225 tests |
| `integrations/google/` | Google Workspace — 113 tools (Gmail 19, Calendar 12, Drive 16, Docs 8, Sheets 10, Slides 6, Meet 27, Tasks 8, Contacts 7); InstalledAppFlow OAuth; 30-min service cache; exponential backoff; pagination; standalone FastMCP server on port 18900; 131 tests |

### Stubs / Placeholders (Not Yet Implemented)

| Module | Description |
|--------|-------------|
| `channels/discord_channel.py` | Discord bot integration |
| `channels/web.py` | Web/HTTP REST channel |

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
- **Cross-Channel Identity** — A unified `pincer_user_id` maps every channel-specific ID to a single identity. N channels can share one identity (`name@ch1:id1=ch2:id2=ch3:id3`). Each channel driver resolves human-readable config values (phone numbers, emails, @usernames) to internal IDs at startup via `resolve_internal_user_id()`.
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
| `pincer auth-google` | Run Google Calendar OAuth consent flow (legacy, 3 tools) |
| `pincer setup-google` | Run Google Workspace OAuth consent flow (113 tools) |
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
| `PINCER_ANTHROPIC_API_KEY` | Anthropic Claude API key (well-known `anthropic`) |
| `PINCER_OPENAI_API_KEY` | OpenAI API key (well-known `openai`; also used for voice transcription) |
| `PINCER_TELEGRAM_BOT_TOKEN` | Telegram bot token (from @BotFather) |

For Grok, Ollama, or any other endpoint, configure a *compatible* provider instead:
`PINCER_OPENAI_COMPATIBLE_PROVIDER` / `PINCER_OPENAI_COMPATIBLE_BASE_URL` (+ optional
`_API_KEY`), or the `PINCER_ANTHROPIC_COMPATIBLE_*` equivalents, then set
`PINCER_DEFAULT_PROVIDER` to that name. `PINCER_FALLBACK_PROVIDERS` (≤3, comma-separated)
enables random failover.

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
| `PINCER_IDENTITY_MAP` | Cross-channel identity mapping — format: `[name@]ch1:id1=ch2:id2[=ch3:id3]`, multiple entries comma-separated |
| `PINCER_MEMORY_BACKEND` | Memory storage engine: `sqlite` (default, local DB) or `mcp` (external MCP server) |
| `PINCER_MEMORY_MCP_SERVER` | MCP server name when `PINCER_MEMORY_BACKEND=mcp` (default: `sqlite-vec-memory`) |
| `PINCER_OPENWEATHERMAP_API_KEY` | Weather for morning briefings |
| `PINCER_NEWSAPI_KEY` | News for morning briefings |

### First-Time Setup

1. `cp .env.example .env` — fill in API keys
2. `pincer auth-google` — one-time Google Calendar consent (legacy, 3 tools)
   — **or** —
   `pincer setup-google` — one-time Google Workspace consent (full 113-tool suite incl. Meet v2)
3. `pincer run` — scan WhatsApp QR on first launch
4. Message yourself on WhatsApp or talk to the bot on Telegram

### Google Workspace

| Variable / File | Purpose |
|----------------|---------|
| `~/.pincer/google_credentials.json` | OAuth 2.0 client ID (from Google Cloud Console, Desktop App type) |
| `~/.pincer/google_workspace_token.json` | OAuth token (created by `pincer setup-google`, `chmod 0o600`) |

When both files exist, all 85 `google__*` tools auto-register on `pincer run`. No env vars required.

---

## Codebase Stats

| Metric | Value |
|--------|-------|
| Source code | ~12,400 lines |
| Test code | ~3,500 lines |
| Total Python files | 58 |
| Test files | 21 |
| Built-in tools | 15 + 85 Google Workspace = 100 |
| Active channels | 7 (Telegram, WhatsApp, Discord, Slack, Email, Signal, Voice) |
