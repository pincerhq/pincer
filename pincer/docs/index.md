# Pincer Wiki

**Pincer** is a personal AI agent framework that you can text on messaging platforms like Telegram. It processes your messages using LLM providers (Anthropic Claude, OpenAI GPT), executes tools on your behalf, and remembers context across conversations.

> *Your personal AI agent. Text it on WhatsApp. It does stuff.*

---

## Table of Contents

### Getting Started
- [Overview & Purpose](overview.md)
- [Installation & Setup](getting-started.md)
- [Configuration Reference](configuration.md)

### Architecture
- [System Architecture](architecture/system-architecture.md)
- [Data Flow](architecture/data-flow.md)
- [Project Structure](architecture/project-structure.md)

### Core Modules
- [Agent Core (ReAct Loop)](modules/agent-core.md)
- [Session Management](modules/session-management.md)
- [LLM Providers](modules/llm-providers.md)
- [Cost Tracking & Budget](modules/cost-tracking.md)

### Tools System
- [Tool Registry](modules/tool-registry.md)
- [Built-in Tools](modules/builtin-tools.md)

### Messaging Channels
- [Channel System](modules/channels.md)
- [Telegram Channel](modules/telegram-channel.md)

### Memory & Context
- [Memory System](modules/memory-system.md)
- [Conversation Summarizer](modules/summarizer.md)

### Security
- [Security Model](modules/security.md)

### Reference
- [CLI Reference](reference/cli.md)
- [Exception Hierarchy](reference/exceptions.md)
- [Environment Variables](reference/environment-variables.md)

---

## Quick Links

| Component | Source | Description |
|-----------|--------|-------------|
| Agent | `src/pincer/core/agent.py` | ReAct loop engine |
| Config | `src/pincer/config.py` | Pydantic settings |
| CLI | `src/pincer/cli.py` | Typer CLI entry point |
| LLM Base | `src/pincer/llm/base.py` | Provider abstraction |
| Tools | `src/pincer/tools/registry.py` | Tool registration & dispatch |
| Memory | `src/pincer/memory/store.py` | SQLite + FTS5 memory |
| Telegram | `src/pincer/channels/telegram.py` | Telegram bot channel |

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.12+ |
| LLM SDKs | `anthropic`, `openai` |
| Messaging | `aiogram` (Telegram) |
| Database | SQLite via `aiosqlite` |
| Config | `pydantic-settings` |
| CLI | `typer` + `rich` |
| Search | Tavily API / DuckDuckGo |
| Browser | Playwright (optional) |
| Build | Hatch |
