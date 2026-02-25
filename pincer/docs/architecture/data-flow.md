# Data Flow

## Message Processing Pipeline

This document traces the complete journey of a user message through Pincer, from arrival to response delivery.

```
User sends message on Telegram
         │
         ▼
┌─────────────────────┐
│  TelegramChannel    │
│  handle_text()      │
│  - Check allowlist  │
│  - Show typing...   │
│  - Build Incoming   │
│    Message          │
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│  CLI on_message()   │
│  - Handle /clear    │
│  - Handle /cost     │
│  - Transcribe voice │
│  - Process files    │
│  - Extract PDF text │
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│  Agent.handle_      │
│  message_stream()   │
│  or handle_message()│
└────────┬────────────┘
         │
         ▼
┌─────────────────────────────────────────────┐
│              ReAct Loop                      │
│                                              │
│  ┌───────────────────────────────────────┐   │
│  │ 1. Load/create session                │   │
│  │ 2. Add user message to session        │   │
│  │ 3. Maybe summarize (if threshold hit) │   │
│  │ 4. Build system prompt + memories     │   │
│  └──────────────┬────────────────────────┘   │
│                 │                             │
│                 ▼                             │
│  ┌──────────────────────────────────────┐    │
│  │ LLM.complete(messages, tools, system)│    │
│  └──────────────┬───────────────────────┘    │
│                 │                             │
│          ┌──────┴──────┐                     │
│          │             │                     │
│     has_tool_calls?    │                     │
│     ┌────┴────┐   ┌───┴───┐                 │
│     │  YES    │   │  NO   │                 │
│     │         │   │       │                 │
│     ▼         │   ▼       │                 │
│  Execute      │  Return   │                 │
│  each tool    │  text as  │                 │
│  via Registry │  final    │                 │
│     │         │  answer   │                 │
│     ▼         │           │                 │
│  Add results  │           │                 │
│  to session   │           │                 │
│     │         │           │                 │
│     └──→ Loop back to LLM │                 │
│         (max iterations)  │                 │
│                           │                 │
└───────────────────────────┘                 │
                                               │
         ┌─────────────────────────────────────┘
         │
         ▼
┌─────────────────────┐
│  Post-processing    │
│  - Save final msg   │
│  - Store memory     │
│  - Record cost      │
│  - Append cost str  │
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│  TelegramChannel    │
│  send_streaming()   │
│  - Edit message     │
│    progressively    │
│  - Split if > 4096  │
│  - Markdown format  │
└─────────────────────┘
         │
         ▼
   User sees response
```

## Streaming vs Non-Streaming

Pincer supports two response modes:

### Streaming (Default for Telegram text)

1. Tool-call iterations use `complete()` (non-streaming) so results are available immediately
2. Only the **final text response** is streamed token-by-token via `stream()`
3. The Telegram channel edits the message in-place as tokens arrive (every 1.5 seconds)
4. If the response exceeds 4096 chars, it splits into multiple messages

### Non-Streaming

Used for voice, photo, and document handlers. The full response is generated before sending.

## File Upload Processing

```
Incoming file
     │
     ├─ Text file (.py, .json, .md, etc.)
     │   → Decode UTF-8
     │   → Inline as ```code block``` (max 30K chars)
     │
     ├─ PDF (.pdf)
     │   → Extract text via pymupdf
     │   → Inline as code block (max 30K chars)
     │
     └─ Binary / Other
         → Save to ~/.pincer/workspace/uploads/
         → Tell LLM the file path for shell_exec processing
```

## Voice Note Processing

```
Voice note (OGG/MP3/WAV)
     │
     ▼
Download from Telegram servers
     │
     ▼
Send to OpenAI Whisper API
     │
     ▼
Transcribed text replaces
original message text
     │
     ▼
Normal message processing
```

## Memory Retrieval Flow

```
User sends: "What was that restaurant we talked about?"
     │
     ▼
Agent._build_system_prompt()
     │
     ▼
MemoryStore.search_text("restaurant talked about")
     │
     ▼
FTS5 query: "restaurant" OR "talked" OR "about"
     │
     ▼
Top 3 matching memories injected into system prompt:
  "[Relevant memories about this user]
   - User asked about Italian restaurant... Assistant recommended..."
     │
     ▼
LLM sees memories as context and responds accordingly
```

## Cost Tracking Flow

```
Every LLM call
     │
     ▼
CostTracker.record()
     │
     ├─ Calculate cost from pricing table
     │   (per 1M tokens: input_rate, output_rate)
     │
     ├─ Check daily budget
     │   └─ If exceeded → raise BudgetExceededError
     │
     └─ INSERT INTO cost_log
        (timestamp, provider, model, tokens, cost, session_id)
```

## Conversation Summarization Flow

```
Session has 20+ messages
     │
     ▼
Summarizer.maybe_summarize()
     │
     ▼
Take older half of messages
     │
     ▼
Send to cheap LLM (claude-haiku)
with summarization prompt
     │
     ▼
Store summary as memory
(category: "conversation_summary")
     │
     ▼
Replace old messages with
system message: "[Previous conversation summary]..."
     │
     ▼
Session now has ~half the messages
+ summary context preserved
```
