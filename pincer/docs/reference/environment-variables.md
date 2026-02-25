# Environment Variables Reference

> **Source**: `src/pincer/config.py`

All configuration is done via environment variables with the `PINCER_` prefix. Variables can be set in the shell or in a `.env` file in the project root.

## LLM Provider Settings

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `PINCER_ANTHROPIC_API_KEY` | string | — | Anthropic API key for Claude models |
| `PINCER_OPENAI_API_KEY` | string | — | OpenAI API key for GPT models |
| `PINCER_LLM_PROVIDER` | enum | auto | `anthropic`, `openai`, or `auto` (auto-detect from available keys) |
| `PINCER_MODEL` | string | `claude-sonnet-4-5-20250929` | Default model name |
| `PINCER_MAX_TOKENS` | int | `8192` | Maximum tokens per LLM response |
| `PINCER_TEMPERATURE` | float | `0.7` | LLM sampling temperature |

### Auto-Detection Logic

When `PINCER_LLM_PROVIDER=auto` (default):
1. If `PINCER_ANTHROPIC_API_KEY` is set → use Anthropic
2. Else if `PINCER_OPENAI_API_KEY` is set → use OpenAI
3. Else → raise `ConfigError`

## Telegram Settings

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `PINCER_TELEGRAM_BOT_TOKEN` | string | — | Bot token from @BotFather (required for Telegram) |
| `PINCER_TELEGRAM_ALLOWED_USERS` | string | `""` | Comma-separated user IDs (empty = allow all) |

## Agent Behavior

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `PINCER_SYSTEM_PROMPT` | string | built-in | Custom system prompt for the agent |
| `PINCER_MAX_TOOL_ITERATIONS` | int | `10` | Maximum tool calls per user message |
| `PINCER_DAILY_BUDGET` | float | `5.0` | Daily cost limit in USD (0 = unlimited) |

## Search Settings

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `PINCER_TAVILY_API_KEY` | string | — | Tavily API key for web search (optional, falls back to DuckDuckGo) |

## Shell Settings

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `PINCER_SHELL_ENABLED` | bool | `true` | Enable/disable shell_exec tool |
| `PINCER_SHELL_TIMEOUT` | int | `30` | Shell command timeout in seconds (max 300) |
| `PINCER_SHELL_REQUIRE_APPROVAL` | bool | `true` | Require user approval before shell commands |

## Memory Settings

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `PINCER_MEMORY_ENABLED` | bool | `true` | Enable/disable long-term memory |
| `PINCER_SUMMARIZER_THRESHOLD` | int | `20` | Messages before auto-summarization triggers |

## Storage & Logging

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `PINCER_DATA_DIR` | path | `~/.pincer` | Data directory for DB, logs, workspace |
| `PINCER_LOG_LEVEL` | string | `INFO` | Console log level |

## Data Directory Layout

When using the default `PINCER_DATA_DIR=~/.pincer`:

```
~/.pincer/
├── pincer.db               # SQLite database (sessions, costs, memories)
├── pincer.log              # Application log file
└── workspace/              # Sandboxed file workspace
    ├── uploads/            # User-uploaded files
    └── exec_output/        # Python execution output
        └── plot_1.png      # Matplotlib figures
```

## Example `.env` File

```bash
# Required: at least one LLM provider
PINCER_ANTHROPIC_API_KEY=sk-ant-...
PINCER_OPENAI_API_KEY=sk-...

# Required: Telegram bot
PINCER_TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
PINCER_TELEGRAM_ALLOWED_USERS=12345,67890

# Optional: search
PINCER_TAVILY_API_KEY=tvly-...

# Optional: behavior
PINCER_MODEL=claude-sonnet-4-5-20250929
PINCER_DAILY_BUDGET=5.0
PINCER_MAX_TOOL_ITERATIONS=10
PINCER_TEMPERATURE=0.7

# Optional: shell
PINCER_SHELL_ENABLED=true
PINCER_SHELL_TIMEOUT=30
PINCER_SHELL_REQUIRE_APPROVAL=true

# Optional: memory
PINCER_MEMORY_ENABLED=true
PINCER_SUMMARIZER_THRESHOLD=20
```
