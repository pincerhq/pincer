# Exception Hierarchy

> **Source**: `src/pincer/exceptions.py`

Pincer defines a structured exception hierarchy so callers can catch errors at the appropriate level of specificity.

## Exception Tree

```
PincerError (base)
  ├── LLMError                 — LLM provider failures
  │     └── BudgetExceededError — Daily budget limit hit
  ├── ToolError                — Tool system failures
  │     └── ToolNotFoundError  — Tool name not in registry
  ├── ChannelError             — Messaging channel failures
  └── ConfigError              — Configuration / env var issues
```

## Exception Details

### `PincerError`

Base exception for all Pincer errors. Catch this to handle any Pincer-specific error.

```python
class PincerError(Exception):
    """Base exception for Pincer."""
```

### `LLMError`

Raised when an LLM provider encounters an unrecoverable error (API failure, authentication error, invalid response format).

```python
class LLMError(PincerError):
    """LLM provider error."""
```

**Raised by:**
- `AnthropicProvider` — after exhausting rate-limit retries
- `OpenAIProvider` — on API errors

### `BudgetExceededError`

Raised when the daily cost budget is exceeded. Contains the current spend and budget limit.

```python
class BudgetExceededError(LLMError):
    def __init__(self, spent: float, limit: float):
        self.spent = spent
        self.limit = limit
        super().__init__(
            f"Daily budget exceeded: ${spent:.2f} / ${limit:.2f}"
        )
```

**Caught by:** `Agent.handle_message()` — returns a friendly budget warning to the user instead of crashing.

### `ToolError`

Raised when a tool execution fails due to a system-level issue (not a tool-logic error).

```python
class ToolError(PincerError):
    """Tool execution error."""
```

### `ToolNotFoundError`

Raised when the agent tries to call a tool that doesn't exist in the registry.

```python
class ToolNotFoundError(ToolError):
    """Tool not found in registry."""
```

**Caught by:** `Agent._execute_tool()` — the error message is fed back to the LLM so it can try a different tool.

### `ChannelError`

Raised for messaging channel failures (connection issues, API errors, message delivery failures).

```python
class ChannelError(PincerError):
    """Channel error."""
```

### `ConfigError`

Raised when configuration is invalid or missing required values.

```python
class ConfigError(PincerError):
    """Configuration error."""
```

## Error Handling Philosophy

Pincer follows a **resilient error handling** strategy:

1. **Tool errors** are never raised to the user — they are captured and fed back to the LLM as error text, allowing the agent to adapt
2. **Budget errors** produce a friendly user-facing message
3. **LLM errors** trigger retries with exponential backoff before failing
4. **Channel errors** are logged but don't crash the bot
5. **Config errors** fail fast at startup with clear messages
