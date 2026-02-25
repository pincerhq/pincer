# CLI Reference

> **Source**: `src/pincer/cli.py`

Pincer is controlled via a Typer-based CLI. The entry point is `pincer` (or `python -m pincer`).

## Commands

### `pincer run`

Start the agent and connect to messaging channels.

```bash
pincer run [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--channel` / `-c` | `telegram` | Messaging channel to use |

**What happens on startup:**

1. Load configuration from environment / `.env` file
2. Ensure data directory exists (`~/.pincer/`)
3. Configure logging (file + console)
4. Initialize SQLite databases (sessions, costs, memory)
5. Create LLM provider (auto-detect or configured)
6. Register all built-in tools
7. Create Agent with all dependencies
8. Start the selected channel
9. Begin message processing loop
10. On Ctrl+C: graceful shutdown of all components

### `pincer config`

Display the current configuration (non-sensitive values).

```bash
pincer config
```

Shows all settings with secrets masked. Useful for verifying environment setup.

### `pincer cost`

Show cost summary for today and all-time.

```bash
pincer cost
```

Output:
```
Cost Summary
  Today:   $0.12
  Total:   $2.56
  Calls:   42
  Tokens:  125,000 in / 15,000 out
  Budget:  $5.00/day
```

## Entry Points

The CLI can be invoked in multiple ways:

```bash
# Via installed console script
pincer run

# Via Python module
python -m pincer run

# Via main.py (development)
python main.py
```

The `pyproject.toml` defines:
```toml
[project.scripts]
pincer = "pincer.cli:app"
```

## Composition Root

The `_run_agent()` async function in `cli.py` serves as the **composition root** — it creates all components and wires them together:

```python
async def _run_agent(channel_name: str):
    settings = Settings()

    # 1. Database layer
    session_mgr = SessionManager(settings.data_dir / "pincer.db")
    cost_tracker = CostTracker(settings.data_dir / "pincer.db", settings.daily_budget)
    memory_store = MemoryStore(settings.data_dir / "pincer.db")

    # 2. LLM provider
    if settings.llm_provider == LLMProvider.ANTHROPIC:
        llm = AnthropicProvider(api_key=settings.anthropic_api_key, model=settings.model)
    elif settings.llm_provider == LLMProvider.OPENAI:
        llm = OpenAIProvider(api_key=settings.openai_api_key, model=settings.model)

    # 3. Tool registry
    tools = ToolRegistry()
    tools.register("web_search", ...)
    tools.register("shell_exec", ...)
    # ... all tools

    # 4. Agent
    agent = Agent(settings, llm, session_mgr, cost_tracker, tools, memory_store, summarizer)

    # 5. Channel
    channel = TelegramChannel(settings)
    await channel.start(on_message)

    # 6. Wait for shutdown
    await shutdown_event.wait()
```

## Logging

Logging is configured at startup:

| Destination | Level | Format |
|-------------|-------|--------|
| Console | INFO | Colored via Rich |
| File (`~/.pincer/pincer.log`) | DEBUG | Timestamped with module name |

```python
logging.basicConfig(level=logging.DEBUG, handlers=[file_handler, console_handler])
```

## Signal Handling

The CLI registers handlers for graceful shutdown:

```python
for sig in (signal.SIGINT, signal.SIGTERM):
    loop.add_signal_handler(sig, lambda: shutdown_event.set())
```

On shutdown:
1. Channel stops polling
2. Browser instance closed
3. LLM client closed
4. Session manager flushes and closes
5. Cost tracker closes
6. Memory store closes
