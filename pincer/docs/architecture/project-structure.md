# Project Structure

## Directory Layout

```
pincer/
├── .env.example              # Template for environment variables
├── .github/
│   └── workflows/
│       └── ci.yml            # GitHub Actions CI (pytest + ruff)
├── .gitignore
├── .python-version           # Python version pin (3.12)
├── Dockerfile                # Container image definition
├── README.md
├── docker-compose.yml        # Docker Compose service definition
├── docs/                     # Documentation (this wiki)
├── main.py                   # Simple entry point (hello world)
├── pyproject.toml            # Package metadata, dependencies, tool config
├── skills/                   # Custom skills directory (empty)
├── src/
│   └── pincer/               # Main source package
│       ├── __init__.py        # Package root, version string
│       ├── __main__.py        # python -m pincer entry point
│       ├── cli.py             # Typer CLI + startup orchestration
│       ├── config.py          # Pydantic settings model
│       ├── exceptions.py      # Exception hierarchy
│       ├── channels/          # Messaging platform adapters
│       │   ├── __init__.py
│       │   ├── base.py        # BaseChannel ABC + IncomingMessage
│       │   ├── telegram.py    # Telegram (aiogram) — implemented
│       │   ├── whatsapp.py    # WhatsApp — placeholder
│       │   ├── discord_channel.py  # Discord — placeholder
│       │   └── web.py         # Web — placeholder
│       ├── core/              # Agent brain
│       │   ├── __init__.py
│       │   ├── agent.py       # ReAct loop, streaming, tool execution
│       │   ├── events.py      # Event handling — placeholder
│       │   └── session.py     # SQLite session manager
│       ├── dashboard/         # Dashboard — placeholder
│       │   ├── __init__.py
│       │   └── templates/
│       ├── llm/               # LLM provider abstraction
│       │   ├── __init__.py    # Public API re-exports
│       │   ├── base.py        # ABC + unified message types
│       │   ├── anthropic_provider.py  # Claude provider
│       │   ├── openai_provider.py     # GPT provider
│       │   ├── ollama_provider.py     # Local models — placeholder
│       │   └── cost_tracker.py        # Per-call cost tracking
│       ├── memory/            # Long-term memory
│       │   ├── __init__.py
│       │   ├── store.py       # SQLite + FTS5 memory store
│       │   └── summarizer.py  # Auto-summarization
│       ├── scheduler/         # Task scheduling — placeholder
│       │   └── __init__.py
│       ├── security/          # Security components — placeholders
│       │   ├── __init__.py
│       │   ├── audit.py       # Audit logging
│       │   ├── doctor.py      # Health checks
│       │   ├── firewall.py    # Firewall rules
│       │   └── rate_limiter.py # Rate limiting
│       └── tools/             # Tool system
│           ├── __init__.py
│           ├── approval.py    # Approval flow — placeholder
│           ├── registry.py    # Tool registration + dispatch
│           ├── sandbox.py     # Sandboxing — placeholder
│           └── builtin/       # Built-in tools
│               ├── __init__.py
│               ├── browser.py       # Playwright browse + screenshot
│               ├── calendar_tool.py # Calendar — placeholder
│               ├── email_tool.py    # Email — placeholder
│               ├── files.py         # Sandboxed file read/write/list
│               ├── python_exec.py   # Isolated Python execution
│               ├── shell.py         # Shell execution with safety
│               ├── transcribe.py    # Whisper voice transcription
│               └── web_search.py    # Tavily / DuckDuckGo search
├── tests/                     # Test suite
│   ├── conftest.py
│   ├── test_agent.py
│   ├── test_config.py
│   ├── test_integration.py
│   ├── test_llm.py
│   ├── test_telegram.py
│   └── test_tools.py
└── uv.lock                   # Dependency lock file
```

## Package Architecture

The package uses `hatchling` as its build backend and is structured as a `src` layout:

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/pincer"]
```

This means:
- Source code lives under `src/pincer/`
- Imports use `from pincer.xxx import yyy`
- Tests use `pythonpath = ["src"]` to find the package

## Module Dependency Graph

```
cli.py (composition root)
  ├── config.py
  ├── core/agent.py
  │     ├── llm/base.py (types)
  │     ├── exceptions.py
  │     ├── core/session.py
  │     │     └── llm/base.py (LLMMessage)
  │     ├── llm/cost_tracker.py
  │     ├── memory/store.py
  │     ├── memory/summarizer.py
  │     └── tools/registry.py
  ├── llm/anthropic_provider.py
  │     ├── llm/base.py
  │     └── exceptions.py
  ├── llm/openai_provider.py
  │     ├── llm/base.py
  │     └── exceptions.py
  ├── tools/builtin/*
  │     └── config.py
  ├── channels/telegram.py
  │     └── channels/base.py
  └── memory/
        ├── store.py
        └── summarizer.py
              ├── llm/base.py
              └── core/session.py
```

## Key Files by Line Count

| File | Lines | Role |
|------|-------|------|
| `cli.py` | 569 | Startup orchestration, tool registration |
| `channels/telegram.py` | 487 | Full Telegram integration |
| `core/agent.py` | 450 | ReAct loop, streaming |
| `memory/store.py` | 382 | Memory, entities, FTS5 |
| `llm/openai_provider.py` | 227 | OpenAI GPT adapter |
| `llm/anthropic_provider.py` | 217 | Anthropic Claude adapter |
| `config.py` | 180 | All settings |
| `core/session.py` | 168 | Session CRUD |
| `llm/cost_tracker.py` | 166 | Cost tracking + budget |
| `tools/builtin/browser.py` | 153 | Playwright tools |
| `tools/registry.py` | 148 | Tool plugin system |
| `tools/builtin/python_exec.py` | 146 | Sandboxed Python |
| `memory/summarizer.py` | 133 | Auto-summarization |
| `tools/builtin/files.py` | 113 | File tools |
| `tools/builtin/shell.py` | 99 | Shell with safety |
| `tools/builtin/web_search.py` | 94 | Web search |
| `channels/base.py` | 90 | Channel ABC |
| `tools/builtin/transcribe.py` | 68 | Voice transcription |
| `exceptions.py` | 49 | Error hierarchy |
| `llm/base.py` | 143 | Provider ABC + types |
