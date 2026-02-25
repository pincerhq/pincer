# Conversation Summarizer

> **Source**: `src/pincer/memory/summarizer.py`

The Summarizer automatically compresses long conversations into summaries, keeping context while reducing token usage.

## Why Summarize?

LLMs have finite context windows and charge per token. As conversations grow:
- Token costs increase linearly
- Older messages become less relevant
- The model may lose focus on the current topic

The summarizer addresses this by replacing older messages with a concise summary.

## Class: `Summarizer`

### Constructor

```python
Summarizer(
    llm: BaseLLMProvider,
    memory_store: MemoryStore | None = None,
    threshold: int = 20,
    model: str = "claude-haiku-4-5-20251001",
)
```

| Parameter | Default | Description |
|-----------|---------|-------------|
| `llm` | required | LLM provider for generating summaries |
| `memory_store` | `None` | If set, saves summaries as memories |
| `threshold` | `20` | Number of messages before summarization triggers |
| `model` | `claude-haiku-4-5-20251001` | Cheap, fast model for summarization |

### Main Method

```python
async def maybe_summarize(self, session: Session) -> bool
```

Returns `True` if summarization was performed.

## Summarization Flow

```
Session has 24 messages
  [sys, user, asst, user, asst, user, asst, ...]
                    │
                    ▼
  Threshold check: 24 > 20 → trigger
                    │
                    ▼
  Split: older half (12 msgs) + newer half (12 msgs)
                    │
                    ▼
  Send older half to cheap LLM with prompt:
    "Summarize this conversation concisely.
     Capture key topics, decisions, action items,
     and any important details."
                    │
                    ▼
  Receive summary text from LLM
                    │
                    ▼
  Store summary as memory (category: "conversation_summary")
                    │
                    ▼
  Replace older messages with system message:
    "[Previous conversation summary]\n{summary}"
                    │
                    ▼
  Session now has ~13 messages:
    [summary_system_msg, ...12 recent msgs]
```

## Summary Prompt

The summarizer uses a focused prompt:

```
Summarize the following conversation between a user and an AI assistant.
Be concise but capture:
- Key topics discussed
- Important decisions or conclusions
- Action items or pending tasks
- Any important details the assistant should remember

Conversation:
{formatted_messages}
```

## Integration with Memory

When `memory_store` is provided, summaries are stored as searchable memories:

```python
if self._memory_store:
    await self._memory_store.store_memory(
        user_id=session.user_id,
        content=summary,
        category="conversation_summary",
    )
```

This means past conversation summaries can be retrieved via FTS5 search in future conversations, even after the original messages are gone.

## Integration with Agent

The agent calls the summarizer before each LLM interaction:

```python
# In Agent.handle_message()
if self._summarizer:
    await self._summarizer.maybe_summarize(session)
```

This ensures conversations stay within manageable sizes without losing important context.

## Cost Efficiency

The summarizer uses the cheapest available model (`claude-haiku-4-5-20251001`) for summarization. A typical summarization call:
- Input: ~2000 tokens (12 messages)
- Output: ~200 tokens (summary)
- Cost: ~$0.002 per summarization

This is much cheaper than sending the full history to an expensive model on every interaction.
