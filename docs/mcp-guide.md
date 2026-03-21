# MCP Client Guide

Pincer supports the [Model Context Protocol (MCP)](https://modelcontextprotocol.io) — the open standard for connecting AI agents to external tools. Any MCP-compliant server (GitHub, Slack, Postgres, file system, browser automation, Home Assistant, and thousands more) can expose its tools to Pincer with a few lines of config.

MCP tools appear identically to built-in tools in the agent loop. The LLM calls them, the user sees the same approval prompts, and everything goes through the same audit log.

---

## Installation

MCP support is an optional extra:

```bash
uv pip install "pincer-agent[mcp]"
# or
pip install "pincer-agent[mcp]"
```

This installs:
- `mcp>=1.8.0` — the official MCP Python SDK
- `PyJWT>=2.8.0` — for MCP server OAuth (if you expose Pincer to external clients)

---

## Quick Start

Create a `pincer.toml` file next to your `.env`:

```toml
[mcp]
enabled = true

[[mcp.servers]]
name      = "filesystem"
transport = "stdio"
command   = "npx"
args      = ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
```

Start Pincer — MCP servers connect automatically at startup:

```bash
pincer run
```

The agent now has filesystem tools available. Send it a message like "list all files in /tmp" and it will call the MCP tool directly.

---

## Configuration

### `pincer.toml` reference

```toml
[mcp]
enabled     = true   # Global kill switch (default: true)
max_servers = 10     # Max concurrent MCP connections (default: 10)
tool_prefix = true   # Prefix tool names with server name (default: true)
                     # Example: github__create_issue, postgres__query

[[mcp.servers]]
name      = "github"               # Unique identifier (alphanumeric + underscore only)
transport = "stdio"                # "stdio" or "streamable-http"
command   = "npx"                  # Executable to run (stdio only)
args      = ["-y", "@modelcontextprotocol/server-github"]
env       = { GITHUB_PERSONAL_ACCESS_TOKEN = "${GITHUB_TOKEN}" }
sandbox   = true                   # Subprocess isolation (default: true)
sandbox_memory_mb = 256            # Memory cap for subprocess in MB (default: 256)
approval_required = ["create_*", "delete_*", "merge_*"]  # Glob patterns
timeout_seconds   = 30             # Per-call timeout (default: 30)
max_retries       = 2              # Reconnect attempts before disabling (default: 2)
enabled           = true           # Can be toggled off without removing (default: true)

[[mcp.servers]]
name      = "postgres"
transport = "stdio"
command   = "npx"
args      = ["-y", "@modelcontextprotocol/server-postgres", "${DATABASE_URL}"]
env       = { DATABASE_URL = "${PINCER_DATABASE_URL}" }
sandbox   = true
approval_required = ["*"]          # Require approval for ALL tools
timeout_seconds   = 60
```

### Transport types

| Transport | When to use | Required fields |
|-----------|-------------|-----------------|
| `stdio` | Local processes (`npx`, Python scripts, Go binaries) | `command`, `args` |
| `streamable-http` | Remote servers, Docker containers | `url` |

### HTTP transport

```toml
[[mcp.servers]]
name      = "remote_tools"
transport = "streamable-http"
url       = "http://localhost:3000/mcp"
headers   = { Authorization = "Bearer ${REMOTE_MCP_TOKEN}" }
approval_required = ["write_*"]
timeout_seconds   = 60
```

---

## Tool naming

When `tool_prefix = true` (the default), each MCP tool is namespaced with its server name using double underscores:

```
github__create_issue
github__list_notifications
postgres__query
filesystem__read_file
```

This prevents collisions when multiple servers expose tools with the same name. If two servers would produce the same final tool name, Pincer refuses to register the duplicate and logs a warning.

To disable prefixing:

```toml
[mcp]
tool_prefix = false
```

> **Warning:** Disabling prefixes on multiple servers risks tool name collisions. Pincer blocks conflicting registrations.

---

## Approval patterns

`approval_required` controls which MCP tools require user confirmation before execution.

| Pattern | Meaning |
|---------|---------|
| `["*"]` | All tools on this server require approval **(default)** |
| `["none"]` | No tools require approval (explicit trust) |
| `["create_*", "delete_*"]` | Only tools matching these globs require approval |
| `["!read_*"]` | All tools EXCEPT those matching the negative glob require approval |

**Priority order:**

1. `["none"]` → never require approval
2. `["*"]` → always require approval
3. Glob patterns (positive `pattern` and negative `!pattern`)
4. MCP tool annotations (`destructiveHint` → require, `readOnlyHint` → don't)
5. **Default: require approval** — fail-safe when nothing matches

---

## Environment variable config

If you prefer not to use `pincer.toml`, configure servers entirely via environment variables:

```bash
PINCER_MCP_ENABLED=true

PINCER_MCP_SERVER_1_NAME=filesystem
PINCER_MCP_SERVER_1_TRANSPORT=stdio
PINCER_MCP_SERVER_1_COMMAND=npx
PINCER_MCP_SERVER_1_ARGS=-y,@modelcontextprotocol/server-filesystem,/tmp

PINCER_MCP_SERVER_2_NAME=github
PINCER_MCP_SERVER_2_TRANSPORT=stdio
PINCER_MCP_SERVER_2_COMMAND=npx
PINCER_MCP_SERVER_2_ARGS=-y,@modelcontextprotocol/server-github
PINCER_MCP_SERVER_2_ENV_GITHUB_PERSONAL_ACCESS_TOKEN=${GITHUB_TOKEN}
PINCER_MCP_SERVER_2_APPROVAL=create_*,delete_*
PINCER_MCP_SERVER_2_TIMEOUT=30
PINCER_MCP_SERVER_2_SANDBOX=true
```

Env var servers are merged with `pincer.toml`. If a server with the same name exists in both, the env var version wins.

---

## CLI commands

### Status and inspection

```bash
# Show all configured servers and their current connection status
pincer mcp list

# Test connection to a specific server (connects, lists tools, disconnects)
pincer mcp test github

# List all registered MCP tools across all connected servers
pincer mcp tools

# Filter to a specific server
pincer mcp tools --server github

# Show live connectivity status
pincer mcp status
```

**`pincer mcp list` output:**

```
MCP Servers (2 configured)

  Name         Transport  Status     Tools  Sandbox  Uptime
  ────────────────────────────────────────────────────────────
  github       stdio      connected     12  yes      3m 42s
  filesystem   stdio      connected      8  yes      3m 42s

  Total tools registered: 20
```

### Manual tool invocation

```bash
# Call a tool directly for debugging (bypasses agent loop)
pincer mcp call github list_notifications
pincer mcp call github search_repositories --arg query=pincer
pincer mcp call postgres query --arg sql="SELECT count(*) FROM users"
```

### Registry: discover and install

```bash
# Search the MCP Registry and ClawHub
pincer mcp search "github"
pincer mcp search "database" --registry mcp
pincer mcp search "automation" --registry clawhub

# Install from registry (AST scan runs automatically)
pincer mcp install github-mcp

# Install with a custom server name
pincer mcp install github-mcp --name my_github

# Install without the security scan (not recommended)
pincer mcp install github-mcp --no-scan

# Scan a package without installing
pincer mcp scan github-mcp

# Remove a server (removes from pincer.toml, cleans up staging)
pincer mcp uninstall github
```

### MCP server (expose Pincer to external clients)

```bash
pincer mcp server start    # Start Pincer's outbound MCP endpoint
pincer mcp server stop     # Stop it
pincer mcp server status   # Show status and connected clients
```

See [mcp-server.md](mcp-server.md) for the full server export guide.

---

## Sandbox isolation

When `sandbox = true` (the default for stdio servers), Pincer runs each MCP server in an isolated subprocess:

1. **Sanitized environment** — the subprocess starts with a minimal base env. Only variables explicitly declared in `env` are passed in. Your `PINCER_*` secrets, `AWS_*` credentials, `OPENAI_*` keys, etc. are never visible to MCP subprocesses.

2. **Isolated working directory** — the subprocess starts in a fresh temporary directory, not your project root.

3. **Memory cap** — `sandbox_memory_mb` sets an upper bound via OS resource limits. Exceeding it causes the subprocess to be killed.

4. **Process group management** — on shutdown, Pincer sends SIGTERM to the entire process group, waits 5 seconds, then sends SIGKILL if the process hasn't exited.

**Blocked env var prefixes** (never passed to MCP subprocesses):

```
PINCER_*   AWS_*    GOOGLE_*    AZURE_*
OPENAI_*   ANTHROPIC_*          DATABASE_*
SECRET_*   SSH_*    GPG_*       GITHUB_TOKEN
```

> **macOS note:** `RLIMIT_AS` memory limits are not enforced on macOS due to a kernel limitation. The env sanitization and working directory isolation still apply. Use Docker for enforced memory limits in production.

Set `sandbox = false` only if the server legitimately needs your full environment:

```toml
[[mcp.servers]]
name    = "my_trusted_server"
sandbox = false   # Only for servers you control and fully trust
```

---

## Health monitoring and reconnection

Pincer runs background health checks every 60 seconds. If a server fails the ping, it attempts reconnection with exponential backoff:

| Attempt | Wait |
|---------|------|
| 1st retry | 2 seconds |
| 2nd retry | 4 seconds |
| After `max_retries` exhausted | Server disabled for session |

A disabled server's tools are deregistered from the agent loop and logged to the audit trail. Restart Pincer to re-enable disabled servers.

---

## Prompt injection protection

MCP tool outputs from external servers are treated as **untrusted data**. Before returning output to the agent, Pincer's security gate scans for common prompt injection patterns:

- `ignore all previous instructions`
- `you are now a ...`
- `<system>` tags
- `IMPORTANT: override`
- `forget everything`
- And more

When a pattern is detected, the output is still returned (to avoid breaking functionality) but prefixed with a warning that tells the LLM to treat the content as data, not instructions. The event is flagged in the audit log.

---

## `pincer doctor` MCP checks

`pincer doctor` includes 11 MCP-specific security checks:

| Check | What it verifies |
|-------|-----------------|
| `mcp_config_valid` | `pincer.toml` parses without errors; all required fields present |
| `mcp_sandbox_enabled` | All stdio servers have `sandbox = true` |
| `mcp_approval_not_bypassed` | No server uses `approval_required = ["none"]` |
| `mcp_no_plaintext_secrets` | `pincer.toml` contains no hardcoded tokens (only `${VAR}` refs) |
| `mcp_tool_count` | Total enabled servers within `max_servers` limit |
| `mcp_env_vars` | All `${VAR}` references in `env` are resolved at runtime |
| `mcp_collisions` | Multiple servers with `tool_prefix = false` risk name collisions |
| `mcp_servers` | stdio `command` executables exist in `PATH` |
| `mcp_oauth_enabled` | MCP server export has OAuth enabled |
| `mcp_server_not_exposed` | Server is not bound to a non-localhost address without auth |
| `mcp_injection_alerts` | Informational — prompt injection events in recent audit log |

---

## Common server examples

### GitHub

```toml
[[mcp.servers]]
name      = "github"
transport = "stdio"
command   = "npx"
args      = ["-y", "@modelcontextprotocol/server-github"]
env       = { GITHUB_PERSONAL_ACCESS_TOKEN = "${GITHUB_TOKEN}" }
approval_required = ["create_*", "delete_*", "merge_*", "update_*"]
```

Requires Node.js (`brew install node`). Get a token at [github.com/settings/tokens](https://github.com/settings/tokens) with `repo` scope.

### PostgreSQL

```toml
[[mcp.servers]]
name      = "postgres"
transport = "stdio"
command   = "npx"
args      = ["-y", "@modelcontextprotocol/server-postgres", "${DATABASE_URL}"]
env       = { DATABASE_URL = "${DATABASE_URL}" }
approval_required = ["*"]
timeout_seconds   = 60
```

### Local filesystem

```toml
[[mcp.servers]]
name      = "filesystem"
transport = "stdio"
command   = "npx"
args      = ["-y", "@modelcontextprotocol/server-filesystem", "/home/user/documents"]
approval_required = ["write_file", "delete_file", "move_file", "create_directory"]
```

### Brave Search

```toml
[[mcp.servers]]
name      = "brave_search"
transport = "stdio"
command   = "npx"
args      = ["-y", "@modelcontextprotocol/server-brave-search"]
env       = { BRAVE_API_KEY = "${BRAVE_API_KEY}" }
approval_required = ["none"]  # Search is read-only; no approval needed
```

### Slack

```toml
[[mcp.servers]]
name      = "slack"
transport = "stdio"
command   = "npx"
args      = ["-y", "@modelcontextprotocol/server-slack"]
env       = { SLACK_BOT_TOKEN = "${SLACK_BOT_TOKEN}", SLACK_TEAM_ID = "${SLACK_TEAM_ID}" }
approval_required = ["post_message", "send_*"]  # Approve sending, allow reading
```

### Home Assistant (remote)

```toml
[[mcp.servers]]
name      = "home_assistant"
transport = "streamable-http"
url       = "http://homeassistant.local:8123/mcp"
headers   = { Authorization = "Bearer ${HA_TOKEN}" }
approval_required = ["*"]  # Approve all home automation actions
timeout_seconds   = 15
```

---

## Troubleshooting

**Server fails to connect at startup**

Pincer logs a warning and continues — a failed MCP server doesn't block startup. Check:

```bash
pincer mcp test github   # Shows the exact error
```

Common causes:
- `npx` not in PATH (`brew install node`)
- Package doesn't exist — run the command manually first
- Missing env var — check `${VAR}` is set in your shell

**Tools not appearing in the agent**

```bash
pincer mcp tools   # See what was registered
```

- Tool names are prefixed: `github__search_repositories`, not `search_repositories`
- Check if the server actually connected: `pincer mcp list`
- If `tool_prefix = false`, check for name collision warnings in logs

**Approval prompt not appearing**

- `["none"]` disables all approvals for that server
- The fail-safe default when no pattern matches is **require approval**
- Check the pattern: `create_*` matches `create_issue` but not `delete_issue`

**Server keeps disconnecting**

- Health checks run every 60 seconds; failures trigger reconnection with backoff
- After `max_retries` exhausted, the server is disabled — restart Pincer to re-enable
- Check `pincer mcp status` for current state

**Environment variable not visible in subprocess**

When `sandbox = true`, only explicitly declared `env` vars are passed to the subprocess:

```toml
env = { MY_VAR = "${MY_VAR}" }  # Declare every env var the server needs
```

**macOS: memory limit not enforced**

This is expected — `RLIMIT_AS` is not supported on macOS. Env sanitization and working directory isolation still apply. Use Docker for enforced limits.
