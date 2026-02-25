# Agent Core (ReAct Loop)

> **Source**: `src/pincer/core/agent.py`

The `Agent` class is the brain of Pincer. It implements a [ReAct](https://arxiv.org/abs/2210.03629) (Reason + Act) loop that processes user messages by iteratively calling the LLM and executing tools until a final text response is produced.

## Class: `Agent`

### Constructor

```python
Agent(
    settings: Settings,
    llm: BaseLLMProvider,
    session_manager: SessionManager,
    cost_tracker: CostTracker,
    tool_registry: ToolRegistry,
    memory_store: MemoryStore | None = None,
    summarizer: Summarizer | None = None,
)
```

All dependencies are injected — the agent never creates its own LLM client or database connection.

### Key Methods

#### `handle_message()` — Synchronous Response

```python
async def handle_message(
    user_id: str,
    channel: str,
    text: str,
    images: list[tuple[bytes, str]] | None = None,
) -> AgentResponse
```

Full pipeline:
1. Get or create session for this user/channel
2. Build `LLMMessage` with text and optional images
3. Run summarizer if conversation is long
4. Build system prompt with relevant memories
5. Enter ReAct loop (up to `max_tool_iterations`)
6. Save final response to session
7. Store exchange as memory for future retrieval

#### `handle_message_stream()` — Streaming Response

```python
async def handle_message_stream(
    user_id: str,
    channel: str,
    text: str,
    images: list[tuple[bytes, str]] | None = None,
) -> AsyncIterator[StreamChunk]
```

Same pipeline, but:
- Tool iterations use `complete()` (non-streaming) for immediate results
- Only the **final text** response is streamed via `stream()`
- Yields `StreamChunk` objects with type and content

## ReAct Loop Detail

```python
for _iteration in range(max_tool_iterations):   # default: 10
    response = await llm.complete(messages, tools, system)

    # Track cost (raises BudgetExceededError if over limit)
    cost = await cost_tracker.record(...)

    if response.has_tool_calls:
        # Save assistant message with tool calls
        # Execute each tool via registry
        # Save tool results as messages
        # Continue loop → LLM sees results
        continue

    # No tool calls → final answer
    final_text = response.content
    break
```

### Safety Mechanisms

| Mechanism | Description |
|-----------|-------------|
| **Max iterations** | Loop capped at `max_tool_iterations` (default 10) |
| **Circuit breaker** | After 3 consecutive tool errors, stops and returns what it has |
| **Budget enforcement** | `BudgetExceededError` halts processing with a friendly message |
| **Orphan cleanup** | Detects orphaned `tool_result` messages and strips them to prevent API errors |
| **Graceful degradation** | If loop exhausts, returns last LLM content or a fallback message |

## Data Types

### `AgentResponse`

```python
@dataclass
class AgentResponse:
    text: str              # Final response text
    cost_usd: float        # Total cost of this interaction
    tool_calls_made: int   # Number of tool calls executed
    model: str             # Model used for the response
```

### `StreamChunk`

```python
@dataclass(frozen=True, slots=True)
class StreamChunk:
    type: StreamEventType  # TEXT, TOOL_START, TOOL_DONE, DONE
    content: str           # Token text or tool name
```

### `StreamEventType`

| Value | Meaning |
|-------|---------|
| `TEXT` | A text token from the streaming response |
| `TOOL_START` | A tool is about to be executed |
| `TOOL_DONE` | A tool finished executing |
| `DONE` | The full response is complete |

## System Prompt Construction

The `_build_system_prompt()` method enriches the base system prompt with relevant memories:

```python
async def _build_system_prompt(self, user_id: str, user_text: str) -> str:
    # 1. Start with configured system prompt
    # 2. Search memories matching user's message (FTS5)
    # 3. Append top 3 memories as context
    return f"{base_prompt}\n\n[Relevant memories about this user]\n{memory_block}"
```

## Tool Execution

The `_execute_tool()` method wraps tool calls with error handling:

```python
async def _execute_tool(self, tool_call, user_id, channel) -> ToolResult:
    try:
        result_text = await self._tools.execute(
            tool_call.name,
            tool_call.arguments,
            context={"user_id": user_id, "channel": channel},
        )
        return ToolResult(tool_call_id=..., content=result_text, is_error=False)
    except ToolNotFoundError:
        return ToolResult(..., content="Error: Tool not found", is_error=True)
    except Exception as e:
        return ToolResult(..., content=f"Error: {e}", is_error=True)
```

Tool errors are **never raised** to the caller — they are captured and fed back to the LLM as error messages so it can adapt.
