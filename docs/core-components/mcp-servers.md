# MCP Servers — stdio and HTTP

Pincer supports two transports for connecting to MCP servers: **stdio** and
**streamable-HTTP**. Every bundled MCP server in the workspace (`pomodoro-mcp`,
`pincer-mcps`, `sqlite-vec-memory-mcp`) implements both. This document explains
how each transport works, when to use it, and how to configure it in `pincer.toml`.

Related: [MCP Client Guide](../guides/mcp-guide.md) · [MCP Server (outbound)](pincer-mcp-server.md)

---

## Quick comparison

| Property | stdio | streamable-HTTP |
|----------|-------|----------------|
| **Process model** | Subprocess per Pincer instance | Standalone persistent server |
| **Communication** | JSON-RPC over stdin/stdout pipes | JSON-RPC over HTTP (`/mcp` endpoint) |
| **State** | Fresh process per run | Persistent across calls |
| **Sandbox** | `MCPSandbox` (env isolation, cwd isolation, SIGTERM→SIGKILL) | Your responsibility (Docker, VM, etc.) |
| **Startup cost** | Process fork + import time | One-time at server start |
| **Network** | None (local IPC) | TCP; can be remote |
| **Config field** | `transport = "stdio"` | `transport = "streamable-http"` |
| **Best for** | Simple tools, quick scripts, local tools | Stateful tools, shared servers, Docker |

---

## stdio transport

### How it works

Pincer launches the MCP server as a child process and communicates over its
stdin/stdout pipes. Each JSON-RPC message is a newline-delimited JSON line.

**Without sandbox** (`sandbox = false`):

```
MCPClientSession._connect_stdio()
    → mcp.client.stdio.stdio_client(StdioServerParameters(...))
        → subprocess.Popen([command, *args], stdin=PIPE, stdout=PIPE, ...)
        → yields (read_stream, write_stream) for ClientSession
```

**With sandbox** (`sandbox = true`, the default):

```
MCPClientSession._connect_stdio_sandboxed()
    → MCPSandbox.start()
        → subprocess.Popen(cmd, start_new_session=True, preexec_fn=_apply_resource_limits)
        → returns (stdin_pipe, stdout_pipe)
    → sandbox_streams(sandbox)
        → _stdout_reader task: readline() → JSONRPCMessage → MemoryObjectReceiveStream
        → _stdin_writer task: SessionMessage → JSON → write() + flush()
        → yields (read_receive, write_send) for ClientSession
```

The `sandbox_streams` async bridge (in `src/pincer/mcp/sandbox.py`) translates
the subprocess's raw binary pipes into anyio memory object streams compatible
with `mcp.ClientSession`.

### Sandbox isolation

When `sandbox = true` (the default), `MCPSandbox` applies three layers of protection:

**1. Sanitized environment**

The subprocess receives a minimal environment — not your full shell. Blocked
prefixes are never passed in, even if declared in `env`:

```
PINCER_*   AWS_*   GOOGLE_*   AZURE_*
OPENAI_*   ANTHROPIC_*        DATABASE_*
SECRET_*   SSH_*   GPG_*      GITHUB_TOKEN (exact match)
```

What *is* passed in (built by `MCPSandbox._build_safe_env()`):
- `PATH` — Python's `bin/` dir, venv `bin/`, `/usr/local/bin`, `/usr/bin`, `/bin`
- `HOME=/tmp`, `TMPDIR=/tmp`, `LANG=en_US.UTF-8`, `PYTHONUNBUFFERED=1`
- `VIRTUAL_ENV` — if a venv is active, so the subprocess can find installed packages
- `PYTHONPATH`, `NODE_PATH`, `NVM_DIR`, `UV_PROJECT_ENVIRONMENT` — runtime passthroughs
- Variables explicitly declared in `env = { ... }` in `pincer.toml` — these are always passed through (but still filtered for the blocked prefixes above)

**2. Isolated working directory**

The subprocess starts in a fresh `tempfile.mkdtemp()` directory, not your project
root. The directory is deleted on `MCPSandbox.stop()`.

**3. Process group management**

`start_new_session=True` creates a new session so the subprocess has its own
process group. On shutdown:

```python
os.killpg(pgid, signal.SIGTERM)   # polite shutdown
# wait up to _SIGTERM_WAIT_SECONDS (5s)
os.killpg(pgid, signal.SIGKILL)   # force kill if still running
```

Child processes spawned by the MCP server are also killed — there are no zombie
processes left behind.

**Memory limits** (`sandbox_memory_mb`)

`sandbox_memory_mb = 0` (the default) disables `RLIMIT_AS`. Set it only for
servers whose binary's virtual address space usage you know is bounded — Rust,
Go, and Node.js routinely map several GB of VA space at startup even when actual
RSS is tiny, causing immediate crashes if the limit is too low.

> macOS note: `RLIMIT_AS` is accepted by the kernel but not enforced on macOS.
> Env sanitization and working directory isolation still apply. Use Docker for
> enforced memory limits.

### `pincer.toml` — stdio configuration

```toml
[[mcp.servers]]
name      = "pomodoro"
transport = "stdio"
command   = "python"
args      = ["-m", "pomodoro_server"]

# Security (all defaults shown)
sandbox            = true    # subprocess isolation — keep true
sandbox_memory_mb  = 0       # 0 = no RLIMIT_AS limit
approval_required  = ["*"]   # require approval for all tools

# Timeouts
timeout         = 30   # per-call timeout in seconds
startup_timeout = 30   # how long to wait for server to become ready

# Resilience
max_retries = 2        # reconnect attempts before disabling server

# Env vars passed to the subprocess (values support ${VAR} interpolation)
env = { MY_API_KEY = "${MY_API_KEY}" }
```

### Running a bundled server in stdio mode

The three workspace MCP servers all default to stdio:

```bash
# pomodoro-mcp
python -m pomodoro_server --transport stdio

# pincer-mcps servers
python translate_server.py --transport stdio
python summarize_server.py --transport stdio
python stock_price_server.py --transport stdio
python openweathermap_server.py --transport stdio
python newsapi_server.py --transport stdio

# sqlite-vec-memory-mcp
python memory_server.py --transport stdio
```

The `--transport` flag defaults to the `TRANSPORT` environment variable, which
defaults to `stdio`. Running without any flag is equivalent to `--transport stdio`.

Connect from `pincer.toml`:

```toml
[[mcp.servers]]
name      = "pomodoro"
transport = "stdio"
command   = "python"
args      = ["-m", "pomodoro_server"]
sandbox   = true

[[mcp.servers]]
name      = "memory"
transport = "stdio"
command   = "python"
args      = ["src/sqlite-vec-memory-mcp/memory_server.py"]
sandbox   = true
env       = { MEMORY_DB_PATH = "${HOME}/.pincer/memory.db" }
```

---

## HTTP (streamable-HTTP) transport

### How it works

The MCP server runs as a standalone HTTP service. Pincer connects to it using
`mcp.client.streamable_http.streamablehttp_client`:

```
MCPClientSession._connect_http()
    → streamablehttp_client(url, headers=..., timeout=...)
        → HTTP POST /mcp  (JSON-RPC over streamable HTTP)
        → yields (read_stream, write_stream, _) for ClientSession
```

There is no subprocess — Pincer is a pure network client. The server process
runs independently and persists between calls.

### How each bundled server starts its HTTP mode

The code pattern is identical across all three workspace servers (from
`src/pomodoro-mcp/pomodoro_server.py`, `src/pincer-mcps/`, and
`src/sqlite-vec-memory-mcp/memory_server.py`):

```python
import argparse
import uvicorn
from fastmcp import FastMCP
from starlette.middleware.cors import CORSMiddleware

mcp = FastMCP(name="my_server")

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--transport",
        choices=["http", "stdio"],
        default=os.environ.get("TRANSPORT", "stdio"),
    )
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=os.environ.get("PORT", 8000))
    args = parser.parse_args()

    if args.transport == "http":
        app = mcp.http_app()                          # FastMCP → Starlette ASGI app
        cors_app = CORSMiddleware(                    # wrap with CORS for browser clients
            app=app,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )
        uvicorn.run(cors_app, host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")                   # MCP SDK stdio loop
```

`mcp.http_app()` returns a Starlette application that exposes the MCP endpoint
at `/mcp`. Uvicorn serves it. The CORSMiddleware is applied so browser-based
MCP clients can connect without preflight issues.

### Running a bundled server in HTTP mode

```bash
# pomodoro-mcp — bind on all interfaces, port 8001
python -m pomodoro_server --transport http --host 0.0.0.0 --port 8001

# sqlite-vec-memory-mcp — bind on localhost only, port 8002
MEMORY_DB_PATH=~/.pincer/memory.db \
python src/sqlite-vec-memory-mcp/memory_server.py --transport http --host 127.0.0.1 --port 8002

# Environment variable equivalents (used by Docker / docker-compose)
TRANSPORT=http HOST=0.0.0.0 PORT=8003 python summarize_server.py
```

The MCP endpoint is always at `http://<host>:<port>/mcp`.

### Docker — HTTP mode

The `sqlite-vec-memory-mcp` package ships a `Dockerfile` and `docker-compose.yml`
optimized for HTTP mode. Key points:

```dockerfile
FROM ghcr.io/astral-sh/uv:latest AS uv
FROM python:3.12-slim
COPY --from=uv /uv /uvx /usr/local/bin/
WORKDIR /app
COPY pyproject.toml memory_server.py ./
RUN uv sync --no-cache
# Pre-download ONNX model at build time so the first request is fast
ARG DEFAULT_MODEL=BAAI/bge-small-en-v1.5
ENV FASTEMBED_CACHE_DIR=/app/models
RUN uv run python -c "from fastembed import TextEmbedding; TextEmbedding('${DEFAULT_MODEL}')"
VOLUME ["/data"]
ENV MEMORY_DB_PATH=/data/memory.db TRANSPORT=http HOST=0.0.0.0 PORT=8000
EXPOSE 8000
CMD ["uv", "run", "python", "memory_server.py"]
```

The model is baked into the image at build time — no download delay on the
first embedding call.

### `pincer.toml` — HTTP configuration

```toml
[[mcp.servers]]
name      = "pomodoro"
transport = "streamable-http"
url       = "http://localhost:8001/mcp"
timeout   = 30
max_retries = 2

[[mcp.servers]]
name      = "memory"
transport = "streamable-http"
url       = "http://localhost:8002/mcp"
timeout   = 60    # embedding can be slow on first call

# Remote server with static auth header
[[mcp.servers]]
name      = "remote_tools"
transport = "streamable-http"
url       = "https://tools.example.com/mcp"
headers   = { Authorization = "Bearer ${REMOTE_MCP_TOKEN}" }
timeout   = 30

# Remote server with OAuth 2.1 PKCE flow
[[mcp.servers]]
name      = "oauth_tools"
transport = "streamable-http"
url       = "https://oauth-tools.example.com/mcp"
oauth_enabled = true
```

### Health check endpoint

Each bundled server also exposes a health route at `/` and `/health`:

```python
@mcp.custom_route("/", methods=["GET"])
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    return JSONResponse({"status": "healthy", "service": "pomodoro-mcp", "version": "0.1.0"})
```

Use this to verify the server is up before connecting Pincer, or to wire it into
a load balancer health check.

```bash
curl http://localhost:8001/health
# {"status": "healthy", "service": "pomodoro-mcp", "version": "0.1.0"}
```

---

## Choosing a transport

**Use stdio when:**

- The tool is simple, stateless, and fast to start (most scripts and API wrappers).
- You want maximum isolation — each Pincer instance gets its own subprocess.
- You don't want to manage a separate server process.
- Development or local use where a persistent server is overkill.

**Use HTTP when:**

- The server maintains state across calls (database connections, in-memory caches,
  long-lived authenticated sessions).
- Multiple Pincer instances (or other MCP clients) need to share the same server.
- You are deploying in Docker or Kubernetes where a persistent service fits the
  model better than a subprocess.
- The server has a slow initialization (e.g. loading a large ONNX model) that
  you want to pay only once.
- You need the server on a different machine from Pincer.

**Mixed deployments** — you can connect to some servers via stdio and others via
HTTP in the same `pincer.toml`:

```toml
# Local, fast, stateless — stdio
[[mcp.servers]]
name      = "brave_search"
transport = "stdio"
command   = "npx"
args      = ["-y", "@modelcontextprotocol/server-brave-search"]
env       = { BRAVE_API_KEY = "${BRAVE_API_KEY}" }

# Shared, stateful, heavy model — HTTP
[[mcp.servers]]
name      = "memory"
transport = "streamable-http"
url       = "http://memory-server:8002/mcp"
timeout   = 60
```

---

## Startup and connection lifecycle

### stdio startup sequence

1. Pincer calls `MCPSandbox.start()` (or `stdio_client()` for non-sandboxed).
2. The subprocess launches. Pincer waits `startup_timeout` seconds for it to become ready.
3. `_wait_for_sandbox_startup()` polls `sandbox.alive` after a 200ms delay — if the process has already exited, the startup error from stderr is surfaced immediately.
4. `ClientSession.__aenter__()` is called outside any timeout scope (it creates persistent background tasks).
5. `session.initialize()` and `session.list_tools()` run within the `timeout` window.
6. On success, tools are registered in Pincer's `ToolRegistry`.

### HTTP startup sequence

1. `streamablehttp_client(url, timeout=...)` creates a persistent HTTP client.
2. `ClientSession.initialize()` performs the MCP handshake via an HTTP POST to `/mcp`.
3. `session.list_tools()` discovers available tools.
4. On success, tools are registered.

No process is launched. If the HTTP server is not reachable, the connection
fails immediately with a `ConnectionError`.

### Health monitoring

Pincer runs a background health loop every 60 seconds. For stdio servers it
calls `session.send_ping()`; for HTTP servers the same ping goes over HTTP.

If the health check fails:
1. `session._connected` is set to `False`.
2. The manager schedules a reconnect with exponential backoff (2s, 4s, …).
3. After `max_retries` exhausted, the server is disabled and its tools
   deregistered for the session. Restart Pincer to re-enable.

---

## Environment variable reference

All bundled servers read these environment variables (command-line flags
override them):

| Variable | Default | Description |
|----------|---------|-------------|
| `TRANSPORT` | `stdio` | Transport mode: `stdio` or `http` |
| `HOST` | `0.0.0.0` | HTTP bind host |
| `PORT` | `8000` | HTTP bind port |

Server-specific variables:

| Server | Variable | Default | Description |
|--------|----------|---------|-------------|
| `sqlite-vec-memory-mcp` | `MEMORY_DB_PATH` | `~/memory.db` | SQLite database path |
| `sqlite-vec-memory-mcp` | `EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | fastembed model name |
| `sqlite-vec-memory-mcp` | `EMBED_DIM` | `384` | Vector dimension (must match model) |
| `sqlite-vec-memory-mcp` | `FASTEMBED_CACHE_DIR` | `~/.cache/fastembed` | Model cache directory |
| `pincer-mcps` (`openweathermap`) | `API_KEY` | — | OpenWeatherMap API key |
| `pincer-mcps` (`newsapi`) | `API_KEY` | — | NewsAPI.org API key |
| `pincer-mcps` (`translate`) | `LIBRETRANSLATE_URL` | — | LibreTranslate instance URL |
