# Memory System

> **Source**: `src/pincer/memory/store.py`

The memory system gives Pincer long-term recall across conversations. It uses SQLite with FTS5 full-text search and optional vector similarity for semantic retrieval.

## Architecture

```
MemoryStore
  ├── memories table        — Searchable memory entries
  │   └── memories_fts      — FTS5 full-text index (porter stemming + unicode)
  ├── entities table        — Named entities (people, places, etc.)
  └── conversations table   — Archived conversation snapshots
```

## Data Types

### `Memory`

```python
@dataclass(frozen=True, slots=True)
class Memory:
    id: str           # UUID
    user_id: str      # User this memory belongs to
    content: str      # The memory text
    category: str     # "general", "exchange", "conversation_summary", etc.
    created_at: float # Unix timestamp
    score: float      # Search relevance score (0.0 if not from search)
```

### `Entity`

```python
@dataclass(frozen=True, slots=True)
class Entity:
    id: str
    user_id: str
    name: str                          # "John Smith"
    type: str                          # "person", "place", "project", etc.
    attributes: dict[str, str]         # {"role": "manager", "company": "Acme"}
    last_seen: float                   # Unix timestamp of last mention
```

## Class: `MemoryStore`

### Initialization

```python
store = MemoryStore(db_path)
await store.initialize()  # Creates tables, indexes, FTS5 triggers
```

### Memory Operations

| Method | Description |
|--------|-------------|
| `store_memory(user_id, content, category, embedding)` | Store a new memory entry |
| `search_text(query, user_id, limit)` | FTS5 full-text search |
| `search_similar(embedding, user_id, limit)` | Vector cosine similarity search |
| `get_recent_memories(user_id, limit, category)` | Most recent memories by timestamp |

### Entity Operations

| Method | Description |
|--------|-------------|
| `store_entity(user_id, name, entity_type, attributes)` | Store or update (upsert) an entity |
| `get_entities(user_id, entity_type)` | Get all entities for a user |

### Conversation Archival

| Method | Description |
|--------|-------------|
| `store_conversation(user_id, channel, messages_json)` | Archive a conversation snapshot |

## Full-Text Search (FTS5)

The primary search mechanism uses SQLite's FTS5 extension with **porter stemming** and **unicode61** tokenizer.

### How It Works

1. When a memory is stored, SQLite triggers automatically populate the `memories_fts` table
2. On search, the query is split into words and joined with `OR` for broad matching
3. Results are ranked by FTS5's built-in relevance scoring

```python
async def search_text(self, query: str, user_id: str | None = None, limit: int = 5):
    words = query.split()
    fts_terms = " OR ".join(f'"{w}"' for w in words)  # "weather" OR "today"

    sql = """
        SELECT m.id, m.user_id, m.content, m.category, m.created_at, rank
        FROM memories_fts f
        JOIN memories m ON m.rowid = f.rowid
        WHERE memories_fts MATCH ? AND m.user_id = ?
        ORDER BY rank
        LIMIT ?
    """
```

### FTS5 Sync Triggers

Three triggers keep the FTS index in sync:

- **After INSERT**: Add new content to FTS
- **After DELETE**: Remove content from FTS
- **After UPDATE**: Delete old + insert new in FTS

## Vector Similarity Search

For semantic search, memories can store embeddings (float32 vectors packed as bytes).

### Storage

```python
await store.store_memory(
    user_id="12345",
    content="User prefers Italian food",
    category="preference",
    embedding=[0.1, 0.2, 0.3, ...]  # Optional embedding vector
)
```

Embeddings are packed as compact `float32` blobs using `struct.pack`.

### Search

```python
results = await store.search_similar(
    embedding=[0.1, 0.2, 0.3, ...],
    user_id="12345",
    limit=5,
)
```

Uses pure-Python **cosine similarity** to rank results:

```python
def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sqrt(sum(x * x for x in a))
    norm_b = sqrt(sum(x * x for x in b))
    return dot / (norm_a * norm_b)
```

> **Note**: Vector search currently loads all embeddings and computes similarity in Python. For large memory stores, the `sqlite-vec` extension (optional dependency) could be used for efficient vector indexing.

## How Memory is Used

### Automatic Exchange Storage

After every agent response, the exchange is stored as a memory:

```python
# In Agent.handle_message()
await memory.store_memory(
    user_id=user_id,
    content=f"User asked: {text[:200]}\nAssistant replied: {final_text[:300]}",
    category="exchange",
)
```

### System Prompt Injection

Before each LLM call, relevant memories are searched and injected into the system prompt:

```python
# In Agent._build_system_prompt()
memories = await memory.search_text(user_text, user_id=user_id, limit=3)
# Appended to system prompt:
# [Relevant memories about this user]
# - User asked about Italian food... Assistant recommended Trattoria...
```

### Conversation Summaries

The [Summarizer](summarizer.md) stores conversation summaries as memories with `category="conversation_summary"`, making them searchable for future context.

## Database Schema

```sql
CREATE TABLE memories (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    content TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    embedding_blob BLOB,
    created_at REAL NOT NULL
);
CREATE INDEX idx_mem_user ON memories(user_id, category);

CREATE VIRTUAL TABLE memories_fts USING fts5(
    content, category,
    content=memories, content_rowid=rowid,
    tokenize='porter unicode61'
);

CREATE TABLE entities (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    attributes_json TEXT NOT NULL DEFAULT '{}',
    last_seen REAL NOT NULL
);
CREATE INDEX idx_ent_user ON entities(user_id, type);

CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    messages_json TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX idx_conv_user ON conversations(user_id, channel);
```
