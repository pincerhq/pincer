# Session Management

> **Source**: `src/pincer/core/session.py`

The session system maintains conversation history for each user on each channel, persisted in SQLite.

## Concepts

- A **session** = one conversation thread for one user on one channel
- Sessions are identified by `{channel}:{user_id}` (e.g., `telegram:12345`)
- Messages are stored as JSON-serialized `LLMMessage` objects
- Sessions are cached in memory and written to SQLite on every change

## Class: `Session`

```python
@dataclass
class Session:
    session_id: str                      # "{channel}:{user_id}"
    user_id: str
    channel: str
    messages: list[LLMMessage]           # Full conversation history
    metadata: dict[str, str]             # Extensible key-value metadata
    created_at: float                    # Unix timestamp
    updated_at: float                    # Unix timestamp
```

## Class: `SessionManager`

### Constructor

```python
SessionManager(db_path: Path, max_messages: int = 50)
```

### Methods

| Method | Description |
|--------|-------------|
| `initialize()` | Open SQLite, create tables and indexes |
| `close()` | Persist all cached sessions, close DB |
| `get_or_create(user_id, channel)` | Load from DB or create new session |
| `add_message(session, message)` | Append message + auto-trim + persist |
| `clear(session)` | Clear all messages from session |

### Auto-Trimming

When a session exceeds `max_messages` (default 50), the oldest non-system messages are trimmed:

```python
if len(session.messages) > self._max_messages:
    # Keep all system messages
    # Remove oldest non-system messages
    # Never start on a tool_result (would be orphaned)
    session.messages = system_msgs + trimmed
```

The trimming logic is careful to never leave an orphaned `tool_result` message (one without its corresponding `tool_use` from the assistant).

## Database Schema

```sql
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    messages TEXT NOT NULL DEFAULT '[]',    -- JSON array of LLMMessage dicts
    metadata TEXT NOT NULL DEFAULT '{}',    -- JSON object
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX idx_session_user ON sessions(user_id, channel);
```

## Caching Strategy

Sessions use a write-through cache:

1. **Read**: Check `_cache` dict first, then query SQLite
2. **Write**: Update `_cache` in memory, then immediately persist to SQLite
3. **Shutdown**: Persist all cached sessions before closing

This means every `add_message()` call triggers a database write, ensuring data is never lost even on crash.

## Message Serialization

Messages are serialized using `LLMMessage.to_dict()` and deserialized with `LLMMessage.from_dict()`:

```python
# Serialize
messages_json = json.dumps([m.to_dict() for m in session.messages])

# Deserialize
messages = [LLMMessage.from_dict(m) for m in json.loads(row[1])]
```

Images are **not** persisted in the session (they would be too large). Only text content, role, tool call IDs, and tool call arguments are stored.
