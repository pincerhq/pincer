# System Architecture

## High-Level Overview

Pincer follows a layered architecture where each layer has a single responsibility and communicates through well-defined interfaces.

```
┌─────────────────────────────────────────────────────────┐
│                   Messaging Channels                     │
│  ┌───────────┐  ┌───────────┐  ┌─────────┐  ┌────────┐ │
│  │ Telegram  │  │ WhatsApp  │  │ Discord │  │  Web   │ │
│  │(implemented)│ │(planned)  │  │(planned)│  │(planned)│ │
│  └─────┬─────┘  └───────────┘  └─────────┘  └────────┘ │
│        │                                                 │
│        ▼                                                 │
│  ┌─────────────────────────────────────────────────────┐ │
│  │              BaseChannel Interface                   │ │
│  │     IncomingMessage → MessageHandler → Response      │ │
│  └──────────────────────┬──────────────────────────────┘ │
└─────────────────────────┼───────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│                     CLI Layer                            │
│  ┌─────────────────────────────────────────────────────┐ │
│  │  cli.py — Typer app                                 │ │
│  │  Commands: run, config, cost                        │ │
│  │  Orchestrates startup, wiring, and shutdown         │ │
│  └──────────────────────┬──────────────────────────────┘ │
└─────────────────────────┼───────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│                    Agent Core                            │
│  ┌─────────────────────────────────────────────────────┐ │
│  │  Agent — ReAct loop engine                          │ │
│  │                                                     │ │
│  │  1. Receive message                                 │ │
│  │  2. Build system prompt (inject memories)           │ │
│  │  3. Call LLM with tools                             │ │
│  │  4. If tool_call → execute → feed result → goto 3   │ │
│  │  5. If text → return to user                        │ │
│  │  6. Save session & store memory                     │ │
│  └───────┬─────────────┬──────────────┬────────────────┘ │
│          │             │              │                   │
│          ▼             ▼              ▼                   │
│  ┌────────────┐ ┌────────────┐ ┌──────────────┐         │
│  │   Session   │ │   Memory   │ │    Cost      │         │
│  │  Manager    │ │   Store    │ │   Tracker    │         │
│  └──────┬──────┘ └─────┬──────┘ └──────┬───────┘         │
│         └──────────────┼───────────────┘                  │
│                        ▼                                  │
│              ┌──────────────────┐                         │
│              │   SQLite (aiosqlite)                       │
│              │   pincer.db                 │              │
│              └──────────────────┘                         │
└─────────────────────────────────────────────────────────┘
                          │
          ┌───────────────┼───────────────┐
          ▼               ▼               ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│  LLM Layer   │ │  Tool System │ │   Security   │
│              │ │              │ │   (planned)  │
│ BaseLLMProv. │ │ ToolRegistry │ │  Firewall    │
│  ├─Anthropic │ │  ├─web_search│ │  Rate Limit  │
│  ├─OpenAI    │ │  ├─shell_exec│ │  Audit Log   │
│  └─Ollama(*) │ │  ├─file_*   │ │              │
│              │ │  ├─browse    │ │              │
│              │ │  ├─screenshot│ │              │
│              │ │  ├─python_exec              │ │
│              │ │  ├─send_file │ │              │
│              │ │  └─send_image│ │              │
└──────────────┘ └──────────────┘ └──────────────┘
```

## Key Design Patterns

### 1. Provider Abstraction (Strategy Pattern)

The `BaseLLMProvider` abstract class defines the interface. Concrete providers (`AnthropicProvider`, `OpenAIProvider`) implement `complete()`, `stream()`, and `close()`. The agent core never imports provider-specific code.

```python
class BaseLLMProvider(ABC):
    async def complete(self, messages, tools, ...) -> LLMResponse: ...
    async def stream(self, messages, ...) -> AsyncIterator[str]: ...
    async def close(self) -> None: ...
```

### 2. Unified Message Types

All providers share the same message types (`LLMMessage`, `ToolCall`, `ToolResult`, `LLMResponse`), preventing provider-specific types from leaking into the core.

### 3. Channel Abstraction

`BaseChannel` defines the interface for messaging platforms. Each channel converts platform-specific messages into `IncomingMessage` objects and handles sending responses back.

### 4. Tool Registry (Plugin Pattern)

Tools are registered at startup with a name, description, JSON schema, and async handler function. The registry generates schemas for the LLM and dispatches calls by name.

### 5. Dependency Injection via CLI

The `_run_agent()` function in `cli.py` acts as the composition root — it creates all components, wires them together, and passes them to the `Agent` constructor. No global state or service locator.

### 6. Async-First

Every I/O operation is async:
- LLM API calls via `AsyncAnthropic` / `AsyncOpenAI`
- Database via `aiosqlite`
- Telegram polling via `aiogram`
- Shell execution via `asyncio.create_subprocess_shell`
- Browser automation via Playwright's async API

## Database Schema

Pincer uses a single SQLite database (`~/.pincer/pincer.db`) with these tables:

| Table | Purpose | Module |
|-------|---------|--------|
| `sessions` | Conversation history per user/channel | `SessionManager` |
| `cost_log` | Per-call cost tracking | `CostTracker` |
| `memories` | Searchable memory entries | `MemoryStore` |
| `memories_fts` | FTS5 full-text index on memories | `MemoryStore` |
| `entities` | Named entities (people, places, etc.) | `MemoryStore` |
| `conversations` | Archived conversation snapshots | `MemoryStore` |

## Component Lifecycle

```
Startup:
  1. Load Settings (from env / .env)
  2. Initialize SessionManager (open DB, create tables)
  3. Initialize CostTracker (open DB, create tables)
  4. Initialize MemoryStore + Summarizer (if enabled)
  5. Create LLM provider (Anthropic or OpenAI)
  6. Register all tools in ToolRegistry
  7. Create Agent (inject all dependencies)
  8. Start channels (Telegram polling)
  9. Enter main loop (await messages)

Shutdown (Ctrl+C):
  1. Stop channel polling
  2. Close browser (if opened)
  3. Close LLM client
  4. Flush & close SessionManager
  5. Close CostTracker
  6. Close MemoryStore
```
