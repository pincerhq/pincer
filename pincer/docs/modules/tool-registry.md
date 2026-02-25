# Tool Registry

> **Source**: `src/pincer/tools/registry.py`

The Tool Registry is a plugin system that manages available tools for the agent. It handles registration, schema generation for LLMs, and dispatching tool calls.

## Class: `ToolRegistry`

### Registration

```python
tools = ToolRegistry()

tools.register(
    name="web_search",
    description="Search the web for current information.",
    handler=web_search,              # async function
    parameters={                      # JSON Schema (optional)
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query"},
        },
        "required": ["query"],
    },
    require_approval=False,           # Whether user must approve (optional)
)
```

If `parameters` is omitted, the registry **auto-generates** a JSON Schema from the handler's Python type hints and docstring.

### Auto-Schema Generation

The `_schema_from_hints()` method inspects the handler function:

```python
async def my_tool(path: str, content: str = "") -> str:
    """Do something.

    path: The file path to process
    content: Optional content to write
    """
```

Generates:
```json
{
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "The file path to process"},
        "content": {"type": "string", "description": "Optional content to write"}
    },
    "required": ["path"]
}
```

**Type mapping:**

| Python | JSON Schema |
|--------|------------|
| `str` | `"string"` |
| `int` | `"integer"` |
| `float` | `"number"` |
| `bool` | `"boolean"` |
| `list` | `"array"` |
| `dict` | `"object"` |

### Schema Export

```python
schemas = tools.get_schemas()
# Returns list of Anthropic-style tool definitions:
# [{"name": "...", "description": "...", "input_schema": {...}}, ...]
```

These schemas are passed to the LLM provider's `complete()` method. The OpenAI provider converts them to function-calling format internally.

### Execution

```python
result = await tools.execute(
    name="web_search",
    arguments={"query": "weather today"},
    context={"user_id": "12345", "channel": "telegram"},
)
```

The execution flow:

1. Look up tool by name (raises `ToolNotFoundError` if missing)
2. If the handler accepts a `context` parameter, inject it automatically
3. Call the handler with the arguments
4. Truncate output if longer than 8000 characters
5. Return result string

### Context Injection

Tools that need to know about the calling user/channel can accept a `context` parameter:

```python
async def send_file(path: str, caption: str = "", context: dict | None = None) -> str:
    user_id = context.get("user_id", "")
    channel = context.get("channel", "")
    # ...
```

The registry detects `context` in the function signature via `inspect.signature()` and injects it automatically. The `context` parameter is excluded from the JSON Schema.

## Class: `ToolDef`

```python
@dataclass
class ToolDef:
    name: str                                  # Unique identifier
    description: str                           # Shown to LLM
    parameters: dict[str, Any]                 # JSON Schema
    handler: Callable[..., Awaitable[str]]     # Async function
    require_approval: bool = False             # User approval needed
```

## Registered Tools

The CLI's `_run_agent()` function registers these tools at startup:

| Tool | Always Available | Description |
|------|:---:|-------------|
| `web_search` | Yes | Search via Tavily or DuckDuckGo |
| `shell_exec` | If `PINCER_SHELL_ENABLED` | Run shell commands with safety checks |
| `file_read` | Yes | Read file from sandbox |
| `file_write` | Yes | Write file in sandbox |
| `file_list` | Yes | List sandbox directory |
| `browse` | If Playwright installed | Navigate to URL, return text |
| `screenshot` | If Playwright installed | Screenshot a web page |
| `python_exec` | Yes | Execute Python code |
| `send_file` | Yes | Send file to user via channel |
| `send_image` | Yes | Send image/GIF inline in chat |
