# MCP Server — Exposing Pincer to External Clients

Pincer can act as an MCP server itself, exposing its tools to any MCP-compatible client — Claude Desktop, Cursor, VS Code, another Pincer instance, or any custom client that speaks the [Model Context Protocol](https://modelcontextprotocol.io).

This is the **outbound** direction of MCP. See [mcp-guide.md](mcp-guide.md) for the **inbound** direction (Pincer consuming external MCP servers).

Pincer supports two modes:

| Mode | Command | Description |
|------|---------|-------------|
| **Embedded** | `pincer run` | MCP server runs inside the full agent — approval prompts go to your phone |
| **Standalone** | `pincer mcp serve` | MCP server only — no agent, no channels, approval via policy/CLI/webhook |

---

## What this enables

| External client | What it can do through Pincer |
|-----------------|-------------------------------|
| Claude Desktop | Call Pincer's web search, email, calendar, memory tools |
| Cursor / VS Code | Trigger shell commands, read/write files, search memory |
| Another Pincer instance | Agent-to-agent delegation — one Pincer delegates tasks to another |
| Custom MCP client | Access any tool Pincer has, including third-party skills |

**The key feature:** approval-gated tools (email send, shell exec, file write, voice calls) still require user confirmation — proxied to your active messaging channel (Telegram, WhatsApp, Signal, etc.). Claude Desktop can request a shell command, and the approval prompt appears on your phone.

---

## Setup

### 1. Install with MCP extras

```bash
uv pip install "pincer-agent[mcp]"
```

### 2. Enable in `pincer.toml`

```toml
[mcp.server]
enabled = true
host    = "127.0.0.1"   # Localhost only (default, recommended)
port    = 18800          # Default port
path    = "/mcp"         # Endpoint path

# Tools to expose (conservative defaults — only read-only tools)
expose_tools = [
    "web_search",
    "email_check",
    "calendar_today",
    "memory_search",
]
```

### 3. Start Pincer

```bash
pincer run
```

The MCP server starts on `http://127.0.0.1:18800/mcp`.

---

## Available tools

All Pincer built-in tools can be exposed. Configure which ones via `expose_tools`:

| Tool name | What it does | Approval required? |
|-----------|-------------|-------------------|
| `web_search` | Search the web via Tavily/DuckDuckGo | No |
| `web_browse` | Navigate URLs and extract content | No |
| `email_check` | Read Gmail inbox | No |
| `email_send` | Draft and send email | **Yes** |
| `calendar_today` | Read Google Calendar | No |
| `calendar_create` | Create calendar events | **Yes** |
| `shell_exec` | Run shell commands | **Yes** |
| `python_exec` | Execute Python code | **Yes** |
| `file_read` | Read local files | No |
| `file_write` | Write local files | **Yes** |
| `memory_search` | Search conversation memory | No |
| `voice_call` | Make phone calls via Twilio | **Yes** |

When an MCP client calls an approval-required tool, the approval prompt goes to the user's active messaging channel (Telegram, WhatsApp, etc.) before the tool executes.

### The `pincer_ask_user` tool

This tool is always exported (it cannot be removed from the expose list). It lets external MCP clients send a question directly to you via your messaging channel and wait for your reply:

```
pincer_ask_user(question="What email address should I send this to?")
```

The user receives the question in Telegram/WhatsApp, replies, and the MCP client gets the response. No other MCP server offers this — it gives external tools a direct line to the human.

---

## Full configuration reference

```toml
[mcp.server]
enabled = true               # Must be explicitly set to true (default: false)
host    = "127.0.0.1"        # Bind address; use 127.0.0.1 for local-only (recommended)
port    = 18800              # Port number
path    = "/mcp"             # URL path for the MCP endpoint

# Tools to expose to MCP clients (whitelist — nothing exposed by default)
expose_tools = [
    "web_search",
    "web_browse",
    "email_check",
    "calendar_today",
    "memory_search",
    "file_read",
    # Add tools you want external clients to access
]
```

There is no separate `approval_tools` list in config — the approval requirement is inherited from Pincer's global tool configuration. Tools that require approval in the agent loop also require approval when called by MCP clients.

---

## Connecting Claude Desktop

Add Pincer to your Claude Desktop MCP config (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "pincer": {
      "url": "http://127.0.0.1:18800/mcp"
    }
  }
}
```

Restart Claude Desktop. Pincer tools appear in the tool list automatically.

> Claude Desktop must run on the same machine as Pincer when using localhost.

---

## Connecting Cursor

Open Cursor settings → MCP → Add server:

```json
{
  "pincer": {
    "url": "http://127.0.0.1:18800/mcp"
  }
}
```

---

## Remote access (non-localhost)

By default, the MCP server only accepts connections from `127.0.0.1`. To expose it to other machines, change the bind address and **enable OAuth**:

```toml
[mcp.server]
enabled = true
host    = "0.0.0.0"   # Accept from any interface
port    = 18800

expose_tools = ["web_search", "memory_search"]
```

> **Never expose the MCP server to the internet without OAuth or a VPN.** Without auth, anyone who can reach the port can call your tools. Use a VPN or SSH tunnel for remote access.

OAuth is built into Pincer's MCP server — see the OAuth section below for configuration.

---

## OAuth 2.1

Pincer's MCP server supports OAuth 2.1 `client_credentials` grant for authenticating external clients.

```toml
[mcp.server]
enabled = true
host    = "0.0.0.0"
port    = 18800

[mcp.server.auth]
enabled         = true
allowed_clients = ["claude-desktop", "cursor"]  # Client ID whitelist
token_expiry_seconds = 3600
```

**Token endpoint:** `POST http://host:18800/oauth/token`

```bash
curl -X POST http://host:18800/oauth/token \
  -d "grant_type=client_credentials" \
  -d "client_id=claude-desktop" \
  -d "client_secret=YOUR_SECRET"
```

Localhost connections (`127.0.0.1`, `::1`, `localhost`) bypass OAuth automatically — no token needed for local clients.

---

## Agent-to-agent: Pincer → Pincer

You can run multiple Pincer instances and have one delegate to another via MCP.

**Instance A** (the "orchestrator" — your primary agent):
```toml
# pincer.toml on machine A
[[mcp.servers]]
name      = "research_agent"
transport = "streamable-http"
url       = "http://192.168.1.100:18800/mcp"  # Machine B's IP
approval_required = ["none"]  # Trust the other Pincer instance
```

**Instance B** (the "specialist" — running on another machine or in Docker):
```toml
# pincer.toml on machine B
[mcp.server]
enabled      = true
host         = "0.0.0.0"
port         = 18800
expose_tools = ["web_search", "web_browse", "memory_search"]
```

Now the orchestrator on machine A can say "search for recent papers on MCP security" and delegate the web search to the specialist on machine B. Each instance maintains its own memory, budget, and audit trail.

---

## Standalone mode — `pincer mcp serve`

Run Pincer's MCP server without launching the full agent. Useful for:
- **Secure MCP gateway** — expose a controlled set of tools to your whole team
- **Headless deployment** — server/CI environment where messaging channels are not needed
- **Development** — test MCP integrations without a full Pincer stack

```bash
# Start standalone server with policy-based auto-approval
pincer mcp serve

# Interactive approval via terminal prompt
pincer mcp serve --approval cli

# Webhook-based external approval
pincer mcp serve --approval webhook

# Override host/port
pincer mcp serve --host 0.0.0.0 --port 9000

# Debug logging
pincer mcp serve --verbose
```

### Approval modes in standalone

| Mode | How it works | Use case |
|------|-------------|----------|
| `policy` (default) | Auto-approve/deny per `[mcp.server.approval_policy]` config | Headless, automated |
| `cli` | Interactive `y/N` prompt in the terminal | Foreground, interactive |
| `webhook` | POST to `mcp.server.webhook_url`, poll for decision | Custom approval UI |

### Approval policy config

```toml
[mcp.server.approval_policy]
read        = "approve"   # auto-approve read-only tools (web_search, file_read, …)
write       = "deny"      # auto-deny write tools (file_write, email_send, …)
destructive = "deny"      # auto-deny destructive tools (shell_exec, python_exec, …)
unknown     = "deny"      # auto-deny unclassified tools
default     = "deny"      # catch-all default
```

### Sampling (standalone LLM access)

Standalone mode can handle MCP sampling requests (LLM inference) directly:

```toml
[mcp.server.sampling]
enabled        = true
model          = "claude-sonnet-4-20250514"
budget_per_day = 5.00    # USD — resets at midnight UTC
max_tokens     = 1024
```

Requires `PINCER_LLM_API_KEY` or `ANTHROPIC_API_KEY` in the environment.
Disabled by default.

---

## CLI management

```bash
# Start standalone MCP server
pincer mcp serve

# Check server export config
pincer mcp server status

# Print Claude Desktop / Cursor client config JSON
pincer mcp server config
```

**`pincer mcp server status` output:**

```
MCP Server — http://127.0.0.1:18800/mcp

  Status:         running
  Uptime:         14m 23s
  Tools exposed:  5 (web_search, email_check, calendar_today, memory_search, pincer_ask_user)
  OAuth:          disabled (localhost only)

  Connected clients: 1
    claude-desktop  connected 8m ago  12 calls
```

---

## Security model

**Principle of least privilege:** The MCP server exposes nothing by default. You explicitly whitelist each tool in `expose_tools`.

**Approval gate preserved:** Approval-required tools always prompt the user through their messaging channel, regardless of whether the call comes from the agent loop or an MCP client. An external tool cannot bypass approval.

**Localhost-first:** The server binds to `127.0.0.1` by default. Changing this requires explicit config and is flagged by `pincer doctor`.

**Audit trail:** Every inbound MCP call is logged to the same audit log as agent loop calls — with timestamp, tool name, argument lengths (not values), and outcome.

**`pincer doctor` checks:**

| Check | What it verifies |
|-------|-----------------|
| `mcp_oauth_enabled` | OAuth is enabled when host is not localhost |
| `mcp_server_not_exposed` | Server is not bound to 0.0.0.0 without auth |

---

## Troubleshooting

**Claude Desktop doesn't see Pincer tools**
- Confirm `pincer run` is running and MCP server started: `pincer mcp server status`
- Check the URL in Claude Desktop config matches your `host:port`
- Restart Claude Desktop after adding the server config

**Tool call returns "tool not found"**
- The tool may not be in `expose_tools` — check `pincer mcp server status`
- All exposed tools are prefixed with `pincer_` in the MCP namespace (e.g., `pincer_web_search`)

**Approval prompt not appearing**
- The approval is sent to your active messaging channel (Telegram, WhatsApp, etc.)
- Make sure Pincer's messaging channel is running and reachable
- Check `pincer doctor` for channel health

**Connection refused from remote machine**
- Default bind is `127.0.0.1` — change to `0.0.0.0` in config
- Check firewall rules: `port 18800` must be open
- Consider using a VPN or SSH tunnel instead of opening the port publicly

**OAuth token rejected**
- Localhost connections bypass OAuth — no token needed if client is on the same machine
- Check `client_id` is in `allowed_clients`
- Token expiry is 3600 seconds by default — request a new one if expired
