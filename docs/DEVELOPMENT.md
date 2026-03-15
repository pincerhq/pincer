# Development Guide

This guide covers local development setup, running tests, dashboard development, debugging the agent loop, and adding skills, channels, and tools.

---

## Prerequisites

| Requirement | Version | Purpose |
|-------------|---------|---------|
| **Python** | >= 3.12 | Core runtime |
| **uv** | Latest | Package manager (recommended) or pip |
| **Node.js** | 18+ (LTS) | Dashboard (React + Vite) |
| **pnpm** | 10+ | Dashboard package manager |

### Install uv

```bash
# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Or via pip
pip install uv
```

### Install Node.js and pnpm

Use [nvm](https://github.com/nvm-sh/nvm), [fnm](https://github.com/Schniz/fnm), or your system package manager. For pnpm:

```bash
corepack enable pnpm
# or: npm install -g pnpm
```

---

## Local Development Setup

### 1. Clone and Install

```bash
git clone https://github.com/pincerhq/pincer.git
cd pincer

# Install Python dependencies (includes dev extras)
uv sync --all-extras

# Or with pip
pip install -e ".[dev,all]"
```

### 2. Environment Configuration

```bash
cp .env.example .env
# Edit .env with your API keys (Anthropic, OpenAI, or Ollama)
```

Minimal `.env` for development:

```env
PINCER_ANTHROPIC_API_KEY=sk-ant-...
PINCER_TELEGRAM_BOT_TOKEN=123456:AAx...
PINCER_TELEGRAM_ALLOWED_USERS=your_telegram_user_id
PINCER_DAILY_BUDGET_USD=5.0
```

### 3. Verify Setup

```bash
pincer doctor    # Config and health check
pincer run       # Start the agent (Ctrl+C to stop)
```

---

## Running Tests

### Full Test Suite

```bash
uv run pytest
# or: pytest
```

### With Coverage

```bash
uv run pytest --cov=pincer --cov-report=term-missing
```

### Specific Test Files or Markers

```bash
uv run pytest tests/test_agent.py
uv run pytest tests/test_skill_loader.py -v
uv run pytest -k "test_approval" --no-cov
```

### Async Tests

Tests use `pytest-asyncio` with `asyncio_mode = "auto"` (configured in `pyproject.toml`). No extra decorators needed for async tests:

```python
async def test_my_feature():
    result = await some_async_function()
    assert result == expected
```

### Test Fixtures

Shared fixtures live in `tests/conftest.py`:

- `settings` — Pincer settings with test values
- `mock_llm` — Mocked LLM provider
- `session_manager` — In-memory session store
- `cost_tracker` — Budget tracker
- `tool_registry` — Registry with sample tool
- `sample_skill_dir` / `malicious_skill_dir` — Skill directories for scanner tests

---

## Dashboard Development

The dashboard is a React + Vite + TypeScript app in `dashboard/`.

### Run Dashboard in Dev Mode

```bash
cd dashboard
pnpm install
pnpm dev
```

- Dashboard: http://localhost:3000
- Vite proxies `/api` to `http://localhost:8080` (Pincer API)

### Run API and Dashboard Together

**Terminal 1 — API:**

```bash
uv run pincer run
```

**Terminal 2 — Dashboard:**

```bash
cd dashboard && pnpm dev
```

### Build and Test Production Build

```bash
cd dashboard
pnpm build
```

The built assets go to `dashboard/dist/`. The main Pincer process serves them when `PINCER_DASHBOARD_DIST` points to that path (Docker sets this automatically).

### Dashboard Scripts

| Command | Purpose |
|---------|---------|
| `pnpm dev` | Dev server with HMR |
| `pnpm build` | Production build |
| `pnpm lint` | ESLint |
| `pnpm preview` | Preview production build |

---

## Debugging the Agent Loop

### Enable Verbose Logging

```bash
PINCER_LOG_LEVEL=DEBUG pincer run
```

### Key Files

| File | Purpose |
|------|---------|
| `src/pincer/core/agent.py` | ReAct loop (~190 LOC) |
| `src/pincer/core/session.py` | Session and message history |
| `src/pincer/llm/base.py` | LLM provider interface |
| `src/pincer/tools/registry.py` | Tool registration and dispatch |

### Agent Flow

1. Message arrives → `ChannelRouter` routes to `Agent.handle_message`
2. Session loaded → `SessionManager.get_or_create`
3. LLM call → `BaseLLMProvider.complete` with tools
4. Tool call? → Execute via `ToolRegistry`, feed result back, repeat
5. Text response? → Return to channel, save session

### Debugging Tips

- **Breakpoints:** Use `breakpoint()` or your IDE debugger; run with `python -m pincer.cli run`
- **Tool calls:** Set `PINCER_LOG_LEVEL=DEBUG` to see tool invocations and results
- **LLM traffic:** Mock `BaseLLMProvider` in tests to inspect prompts
- **Session state:** Session DB path is under `PINCER_DATA_DIR`; inspect with SQLite tools

---

## Adding and Testing Skills

### Skill Structure

```
skills/my_skill/
├── skill.yaml      # Manifest (name, version, permissions)
├── main.py         # Tool definitions
└── requirements.txt  # Optional pip deps
```

### Create a New Skill

1. Create `skills/my_skill/skill.yaml`:

```yaml
name: my_skill
version: 0.1.0
description: Does something useful
author: your-name
permissions: [network]
```

2. Create `skills/my_skill/main.py`:

```python
from pincer.tools import tool

@tool(name="my_tool", description="Description for the LLM")
async def my_tool(arg: str) -> str:
    return f"Result: {arg}"
```

3. Restart Pincer. The agent will load the skill automatically.

### Test a Skill

```bash
# Security scan before install
pincer skills scan ./skills/my_skill

# List loaded skills
pincer skills list

# Run agent and ask it to use your tool
pincer run
# Or: pincer chat (CLI chat for quick testing)
```

### Skill Tests

See `tests/test_skill_loader.py` and `tests/test_skill_scanner.py` for patterns. Use `sample_skill_dir` and `malicious_skill_dir` fixtures from `conftest.py`.

---

## Adding and Testing Channels

### Channel Structure

Channels live in `src/pincer/channels/`. Each channel implements the router interface.

### Create a New Channel

1. Add `src/pincer/channels/my_channel.py`
2. Implement the channel interface (see `telegram.py` or `discord.py` as reference)
3. Register in `src/pincer/channels/router.py`
4. Add config in `src/pincer/config.py` if needed

### Test a Channel

- Unit tests: mock the external API (e.g. `tests/test_telegram.py`, `tests/test_discord.py`)
- Integration: run `pincer run --channel my_channel` with real credentials

**Important:** Open a [GitHub Discussion](https://github.com/pincerhq/pincer/discussions) before implementing a new channel so we can align on design.

---

## Adding and Testing Tools

### Built-in Tools

Located in `src/pincer/tools/builtin/`. Each tool is a module with async handlers.

### Register a Tool

```python
# In a skill or builtin module
from pincer.tools import tool

@tool(
    name="my_tool",
    description="What the LLM sees",
    requires_approval=True,  # User must approve in chat
)
async def my_tool(param: str) -> str:
    return f"Done: {param}"
```

### Test a Tool

- Unit test: call the handler directly with mocked dependencies
- Integration: use `pincer chat` and ask the agent to invoke the tool

See `tests/test_tools.py` and `tests/test_sandbox.py` for examples.

---

## Linting and Type Checking

```bash
uv run ruff check src/ tests/
uv run ruff format src/ tests/
uv run mypy src/
```

CI runs these on every push. See [CONTRIBUTING.md](CONTRIBUTING.md) for full code style guidelines.

---

## Quick Reference

| Task | Command |
|------|---------|
| Run agent | `uv run pincer run` |
| Run tests | `uv run pytest` |
| Lint | `uv run ruff check .` |
| Format | `uv run ruff format .` |
| Type check | `uv run mypy src/` |
| Dashboard dev | `cd dashboard && pnpm dev` |
| Doctor | `uv run pincer doctor` |
