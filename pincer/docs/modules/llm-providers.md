# LLM Providers

> **Source**: `src/pincer/llm/`

Pincer abstracts LLM providers behind a common interface so the agent core never depends on Anthropic or OpenAI specifics.

## Architecture

```
BaseLLMProvider (ABC)
  ├── AnthropicProvider  — Claude models via Anthropic SDK
  ├── OpenAIProvider     — GPT models via OpenAI SDK
  └── OllamaProvider     — Local models (placeholder)
```

## Base Interface

> **Source**: `src/pincer/llm/base.py`

### `BaseLLMProvider`

```python
class BaseLLMProvider(ABC):
    async def complete(
        self,
        messages: list[LLMMessage],
        tools: list[dict] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
    ) -> LLMResponse: ...

    async def stream(
        self,
        messages: list[LLMMessage],
        tools: list[dict] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
    ) -> AsyncIterator[str]: ...

    async def close(self) -> None: ...
```

### Unified Message Types

| Type | Description |
|------|-------------|
| `MessageRole` | Enum: `SYSTEM`, `USER`, `ASSISTANT`, `TOOL_RESULT` |
| `LLMMessage` | Message with role, content, optional images and tool calls |
| `ImageContent` | Base64-encoded image with media type |
| `ToolCall` | LLM's request to invoke a tool (id, name, arguments) |
| `ToolResult` | Result of tool execution (tool_call_id, content, is_error) |
| `LLMResponse` | Complete response with content, tool calls, token usage |

### `LLMResponse`

```python
@dataclass(slots=True)
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0
```

---

## Anthropic Provider

> **Source**: `src/pincer/llm/anthropic_provider.py`

Uses the `anthropic` SDK with `AsyncAnthropic` client.

### Features

- **Tool use**: Converts tool schemas to Anthropic's native format
- **Streaming**: Uses `messages.stream()` with `text_stream`
- **Vision**: Passes base64 images as `image` content blocks
- **Rate limit retry**: Exponential backoff (up to 3 retries, max 60s wait)

### Message Conversion

The provider converts unified `LLMMessage` objects to Anthropic's API format:

| Unified Role | Anthropic Format |
|-------------|-----------------|
| `SYSTEM` | Passed as `system` parameter (not in messages array) |
| `USER` | `{"role": "user", "content": "..."}` |
| `ASSISTANT` | `{"role": "assistant", "content": "..."}` |
| `ASSISTANT` (with tools) | Content blocks: `[{"type": "text"}, {"type": "tool_use"}]` |
| `TOOL_RESULT` | `{"role": "user", "content": [{"type": "tool_result"}]}` |
| User with images | Content parts: `[{"type": "image"}, {"type": "text"}]` |

### Rate Limit Handling

```python
async def _call_with_retry(self, kwargs, max_retries=3):
    for attempt in range(max_retries):
        try:
            return await self._client.messages.create(**kwargs)
        except RateLimitError as e:
            retry_after = float(e.response.headers.get("retry-after", "5"))
            wait = min(retry_after * (2**attempt), 60)
            await asyncio.sleep(wait)
    raise LLMError("Exhausted retries")
```

---

## OpenAI Provider

> **Source**: `src/pincer/llm/openai_provider.py`

Uses the `openai` SDK with `AsyncOpenAI` client.

### Features

- **Function calling**: Converts Anthropic-style tool schemas to OpenAI function format
- **Streaming**: Uses `stream=True` with chunked delta processing
- **Vision**: Passes images as `image_url` content parts with base64 data URIs
- **Model mapping**: Translates Claude model names to GPT equivalents

### Model Mapping

If a Claude model name is used with the OpenAI provider, it's automatically mapped:

| Claude Model | OpenAI Equivalent |
|-------------|------------------|
| `claude-sonnet-4-5-20250929` | `gpt-4o` |
| `claude-haiku-4-5-20251001` | `gpt-4o-mini` |

Any unrecognized model name is passed through as-is.

### Tool Schema Conversion

Anthropic uses `input_schema` at the top level; OpenAI wraps tools in a `function` object:

```python
# Anthropic format (used internally by Pincer)
{"name": "web_search", "description": "...", "input_schema": {...}}

# OpenAI format (converted automatically)
{"type": "function", "function": {"name": "web_search", "description": "...", "parameters": {...}}}
```

### Message Conversion

| Unified Role | OpenAI Format |
|-------------|--------------|
| `SYSTEM` | `{"role": "system", "content": "..."}` |
| `USER` | `{"role": "user", "content": "..."}` |
| `ASSISTANT` | `{"role": "assistant", "content": "..."}` |
| `ASSISTANT` (with tools) | `{"role": "assistant", "tool_calls": [...]}` |
| `TOOL_RESULT` | `{"role": "tool", "tool_call_id": "...", "content": "..."}` |
| User with images | Content: `[{"type": "image_url"}, {"type": "text"}]` |

---

## Adding a New Provider

To add a new LLM provider:

1. Create `src/pincer/llm/my_provider.py`
2. Implement `BaseLLMProvider` with `complete()`, `stream()`, and `close()`
3. Handle message conversion from `LLMMessage` to your API format
4. Parse responses into `LLMResponse` with tool calls and token usage
5. Add a new `LLMProvider` enum value in `config.py`
6. Add the initialization branch in `cli.py`'s `_run_agent()` function
