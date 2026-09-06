# Pincer CLI Reference

Complete command reference across all sprints (1–13). For the full list of tools the agent itself can call, see `docs/TOOLS_CATALOG.md` in the repository — **304 first-party tools + unlimited via MCP**.

## Global

| Command | Description | Sprint |
|---------|-------------|--------|
| `pincer --help` | Show all available commands | 1 |
| `pincer --version` | Show Pincer version | 1 |

## Core Commands

| Command | Description | Sprint |
|---------|-------------|--------|
| `pincer init` | Interactive setup wizard (provider, keys, channels, .env) | 4 |
| `pincer run` | Start agent (all configured channels + API server) | 1 |
| `pincer run tasks` | Standalone background-task worker only — no channels/API/MCP export (requires `PINCER_TASK_BROKER=redis`) | 14 |
| `pincer chat` | CLI chat interface (no messaging app needed) | 4 |
| `pincer config` | Show current configuration (masked secrets) | 1 |
| `pincer whatsapp setup` | Pair WhatsApp via QR code | 3 |
| `pincer google auth` | Google Calendar OAuth consent flow (legacy, 3 tools) | 3 |
| `pincer google setup` | Google Workspace OAuth consent flow (113 tools incl. Meet v2) | 9 |
| `ms365-mcp-setup` | Microsoft 365 device code auth flow, default identity (62 tools) | 10 |
| `pincer signal setup` | Open the Signal QR-code link to register/link a device (signal-cli sidecar) | 7.5 |
| `pincer signal status` | Check Signal API health and registered accounts | 7.5 |
| `pincer signal test <recipient>` | Send a test message via Signal | 7.5 |
| `pincer slack setup` | Interactive Slack bot token setup (71 tools) | 11 |

## Cost & Budget

| Command | Description | Sprint |
|---------|-------------|--------|
| `pincer cost` | Show today's spending summary | 1 |
| `pincer cost --days 7` | Show spending for last N days | 5 |
| `pincer cost --by-model` | Breakdown by LLM model | 5 |
| `pincer cost --by-tool` | Breakdown by tool | 5 |
| `pincer cost --export costs.json` | Export cost data to JSON | 5 |

## Security & Audit

| Command | Description | Sprint |
|---------|-------------|--------|
| `pincer doctor` | Run 25+ security checks, traffic-light report | 5 |
| `pincer doctor --json` | Output doctor report as JSON | 5 |
| `pincer audit` | View last 50 audit log entries | 5 |
| `pincer audit --limit 100` | View last N entries | 5 |
| `pincer audit --action tool_call` | Filter by action type | 5 |
| `pincer audit --user USER_ID` | Filter by user | 5 |
| `pincer audit --since 2026-02-20` | Filter from date | 5 |
| `pincer audit --export audit.json` | Export audit log to JSON | 5 |

### Audit Action Types

`tool_call`, `llm_request`, `llm_response`, `file_read`, `file_write`,
`network_request`, `skill_execute`, `auth_attempt`, `config_change`,
`budget_alert`, `rate_limit_hit`, `message_received`, `message_sent`, `error`

## Skills

There is no `pincer skills` CLI command group. Skills are discovered purely from
the filesystem — drop a `SKILL.md`-containing directory into `src/pincer/skills/`
(bundled, shipped inside the package) or `~/.pincer/skills/` (user) and restart.
See the [Skills Guide](../guides/skills-guide.md).

## Memory

| Command | Description | Sprint |
|---------|-------------|--------|
| `pincer memory search <query>` | Search conversation memory | 5 |
| `pincer memory stats` | Show memory usage stats | 5 |
| `pincer memory clear --user USER_ID` | Clear memory for a user | 5 |
| `pincer memory export --user USER_ID` | Export user memories to JSON | 5 |

Each command prints a `Backend:` line showing which storage engine is active for the current run:

```
Backend: sqlite  path=/path/to/pincer.db
```
or
```
Backend: mcp  server=sqlite-vec-memory
```

When `PINCER_MEMORY_BACKEND=mcp`, the command connects directly to the configured MCP memory server (same process as `pincer run`). The MCP server must be reachable — if it is not found in `pincer.toml` or fails to connect, the command exits with an error.

### Memory backend configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `PINCER_MEMORY_BACKEND` | `sqlite` | Storage engine: `sqlite` (local DB file) or `mcp` (external MCP server) |
| `PINCER_MEMORY_MCP_SERVER` | `sqlite-vec-memory` | MCP server name used when `PINCER_MEMORY_BACKEND=mcp` |

The `sqlite-vec-memory` MCP server ships bundled with Pincer (see `src/sqlite-vec-memory-mcp/`). To enable it, add it to your `pincer.toml`:

```toml
[[mcp.servers]]
name      = "sqlite-vec-memory"
transport = "stdio"
command   = "uv"
args      = ["run", "--project", "src/sqlite-vec-memory-mcp", "memory-server"]
```

Then switch the memory backend:

```bash
PINCER_MEMORY_BACKEND=mcp pincer run
```

## Proactive Agent / Background Tasks

| Command | Description | Sprint |
|---------|-------------|--------|
| `pincer schedule list` | List all scheduled tasks (read-only, all users) | 5 |
| `pincer run tasks` | Standalone background-task worker for horizontal scaling | 14 |

Scheduled tasks now run through a durable, retryable [repid](https://pypi.org/project/repid/)
actor system instead of an in-process fire-loop, and are created, removed,
and toggled entirely via chat (`schedule_create`/`list`/`remove`/`toggle`
tools) rather than the CLI. See [Background Tasks](../core-components/background-tasks.md)
for the full architecture, deployment topologies (single-process vs.
Redis-backed split worker), and config reference.

## Google Workspace

| Command | Description | Sprint |
|---------|-------------|--------|
| `pincer google setup` | One-time OAuth consent — opens browser, saves token to `~/.pincer/google_workspace_token.json` | 9 |

### What `pincer google setup` does

1. Verifies `~/.pincer/google_credentials.json` (or `data/google_credentials.json`) — shows instructions if missing
2. Launches `InstalledAppFlow` — opens browser at Google consent page
3. Requests all scopes covering Gmail, Calendar, Drive, Docs, Sheets, Slides, Meet, Tasks, and Contacts/People API
4. Saves the token with `chmod 0o600`
5. Reports: `Google Workspace tools enabled (113 tools)`

After setup, the 113 `google__*` tools are auto-registered every time `pincer run` is started — no further configuration needed. Full catalog: see `docs/TOOLS_CATALOG.md` in the repository.

### Getting `google_credentials.json`

1. Open [Google Cloud Console](https://console.cloud.google.com)
2. Create/select a project → APIs & Services → Enable APIs (Gmail, Calendar, Drive, Docs, Sheets, Slides, Tasks, People)
3. Credentials → Create → OAuth 2.0 Client ID → Desktop App
4. Download JSON → save as `~/.pincer/google_credentials.json`

## Microsoft 365

| Command | Description |
|---------|-------------|
| `ms365-mcp-setup` | One-time device code auth for the `"default"` identity slot — reads credentials from `.env`, displays device code, caches token under `~/.pincer/ms365_mcp/` |

### What `ms365-mcp-setup` does

1. Reads `PINCER_MS365_CLIENT_ID` and `PINCER_MS365_TENANT_ID` from environment or `.env` file
2. Exits with instructions if `PINCER_MS365_CLIENT_ID` is not set
3. Starts MSAL device code flow — prints `https://microsoft.com/devicelogin` + one-time code
4. Waits while you complete sign-in in a browser
5. Caches the token at `~/.pincer/ms365_mcp/default_token_cache.json` (mode `0600`)
6. Reports: `Microsoft 365 authenticated! … 62 Microsoft 365 tools are now available under the 'default' identity`

This is optional — ms365-mcp authenticates every identity lazily on its own
first tool call, so this step isn't required for Pincer's normal multi-user
operation. After setup, start the MCP server with `ms365-mcp-run --transport http`.
See the [Microsoft 365 MCP Guide](../guides/ms365-mcp.md) for the full
tool catalog and multi-user auth flow.

## Slack Native

The Slack *channel* (bot that responds to @mentions and DMs) and the Slack *native tools* (71 tools for reading/posting messages, managing channels, users, files) are both enabled by setting `PINCER_SLACK_BOT_TOKEN`.

| Requirement | For |
|---|---|
| `PINCER_SLACK_BOT_TOKEN=xoxb-…` | All 71 Slack tools + channel |
| `PINCER_SLACK_USER_TOKEN=xoxp-…` (optional) | `slack__search_messages` and `slack__search_files` (full-text search requires a user token per Slack API) |

Full catalog: see `docs/TOOLS_CATALOG.md` in the repository.

## MCP

| Command | Description |
|---------|-------------|
| `pincer mcp list` | Connected MCP servers and status |
| `pincer mcp tools` | List every MCP tool currently registered |
| `pincer mcp test <server>` | Smoke-test a specific MCP server connection |
| `pincer mcp call <server> <tool> --arg v` | Invoke a tool directly |
| `pincer mcp status` | Show live connectivity status for all configured MCP servers |
| `pincer mcp search <query>` | Search MCP Registry and ClawHub for available MCP servers |
| `pincer mcp install <name>` | Install an MCP server: download, scan, and add to `pincer.toml` |
| `pincer mcp scan <path>` | Run an AST security scan on a local directory or installed package |
| `pincer mcp uninstall <name>` | Remove an MCP server from `pincer.toml` and clean up staging files |
| `pincer mcp serve` | Start a standalone Pincer MCP server (no full agent required) |
| `pincer mcp server status` | Show whether Pincer's outbound MCP export server is enabled and its config |
| `pincer mcp server config` | Print MCP client config JSON for Claude Desktop / Cursor |

See [MCP Guide](../guides/mcp-guide.md) for the full MCP client documentation.

### Getting an Azure App client ID

1. Open [Azure App Registrations](https://portal.azure.com/#blade/Microsoft_AAD_RegisteredApps/ApplicationsListBlade)
2. **New registration** → name it, choose account type, leave redirect URI empty
3. Under **Authentication** → **Advanced settings** → enable **Allow public client flows**
4. Copy the **Application (client) ID**

See [Microsoft 365 MCP Guide](../guides/ms365-mcp.md) for the full walkthrough.

## Environment Variables

All environment variables use the `PINCER_` prefix. See `.env.example` for the
complete list. Key variables added in Sprint 5:

| Variable | Default | Description |
|----------|---------|-------------|
| `PINCER_DASHBOARD_TOKEN` | (empty) | Bearer token for API auth |
| `PINCER_DASHBOARD_HOST` | `127.0.0.1` | API server bind host |
| `PINCER_DASHBOARD_PORT` | `8080` | API server port |
| `PINCER_AUDIT_DISABLED` | `false` | Disable audit logging |
| `PINCER_RATE_MESSAGES_PER_MIN` | `30` | Per-user message rate limit |
| `PINCER_RATE_TOOLS_PER_MIN` | `20` | Per-user tool call rate limit |
| `PINCER_MAX_CONCURRENT_LLM` | `5` | Max concurrent LLM requests |

### Background tasks

| Variable | Default | Description |
|----------|---------|-------------|
| `PINCER_TASK_BROKER` | `memory` | `memory` (single-process) or `redis` (required for `pincer run tasks`) |
| `PINCER_TASK_BROKER_URL` | (empty) | Broker URL, e.g. `redis://localhost:6379/0` (required when `task_broker=redis`) |
| `PINCER_TASK_MAX_RETRIES` | `3` | Max attempts for a background task actor before giving up |
| `PINCER_TASK_POLL_INTERVAL` | `60` | Seconds between checks for due cron schedules |
| `PINCER_SCHEDULE_TOOL_ENABLED` | `true` | Enable the `schedule_create`/`list`/`remove`/`toggle` chat tools |
| `PINCER_MAX_SCHEDULES_PER_USER` | `20` | Max active recurring schedules per user |
| `PINCER_MIN_SCHEDULE_INTERVAL_MINUTES` | `15` | Reject schedules that would fire more often than this |

See [Background Tasks](../core-components/background-tasks.md) for details.

### Identity map

| Variable | Format | Description |
|----------|--------|-------------|
| `PINCER_IDENTITY_MAP` | See below | Cross-channel user identity mappings |

**Format:** `[name@]channel1:id1=channel2:id2[=channel3:id3...]` — multiple identities separated by commas.

```bash
# Two channels, named canonical ID (recommended)
PINCER_IDENTITY_MAP=john@telegram:johnDoe=whatsapp:491234567890

# Three channels in one identity
PINCER_IDENTITY_MAP=john@telegram:johnDoe=whatsapp:491234567890=signal:491234567890

# Multiple people
PINCER_IDENTITY_MAP=john@telegram:johnDoe=whatsapp:491234567890,jane@telegram:janeAustin=whatsapp:491111111111

# Hash-based ID (auto-generated, backward-compatible)
PINCER_IDENTITY_MAP=telegram:12345=whatsapp:491234567890
```

The optional `name@` prefix sets the `pincer_user_id` used as the memory tag (e.g. `user:john`). Named IDs are stable across DB rebuilds; hash IDs (`usr_abc123...`) are auto-generated and opaque. Channel-specific identifiers (email addresses for Slack, phone numbers for WhatsApp, @usernames for Telegram) are resolved to internal IDs by each channel driver at startup before being stored.

#### TOML alternative for large rosters

For a roster too large to comfortably manage as one `PINCER_IDENTITY_MAP` string, the same mapping can be expressed as an `[identity]` section in `pincer.toml` / `pincer.local.toml` instead — one table per person, keyed by their `pincer_user_id`:

```toml
[identity.johndoe]
display_name      = "John Doe"
email             = "john.doe@domain.tld"
timezone          = "Europe/Berlin"
preferred_channel = "telegram"

[[identity.johndoe.channels]]
channel         = "telegram"
channel_user_id = 123456

[identity.jane]
display_name      = "Jane Austin"
preferred_channel = "whatsapp"

[[identity.jane.channels]]
channel         = "telegram"
channel_user_id = "janeAustin"

[[identity.jane.channels]]
channel         = "whatsapp"
channel_user_id = "491111111111"
```

Unlike `PINCER_IDENTITY_MAP`, a person may have just one `[[...channels]]` entry — useful for pre-registering a roster incrementally as new channel IDs become known. `pincer.local.toml`'s `[identity]` section merges into `pincer.toml`'s per person, per field, appending new person keys and leaving unspecified fields inherited from the base entry — see [Local overrides — pincer.local.toml](../guides/mcp-guide.md#local-overrides--pincerlocaltoml) for the same merge pattern applied to `[[mcp.servers]]`. As with those entries, `channels` is replaced wholesale by an override rather than merged per-channel.

**`PINCER_IDENTITY_MAP`, when set (non-empty), always fully overrides the TOML `[identity]` section** — the two are not merged; the TOML section is simply ignored while the env var is set.

#### Guest access control

Once an identity map (`PINCER_IDENTITY_MAP` or TOML `[identity]`) is configured, senders on Telegram, WhatsApp, and Signal whose `channel:user_id` is *not* in the map are treated as guests:

| Variable | Default | Description |
|----------|---------|-------------|
| `PINCER_TELEGRAM_GUESTS_ALLOWED` | `false` | Let unmapped Telegram senders through with a disposable identity instead of rejecting them |
| `PINCER_WHATSAPP_GUESTS_ALLOWED` | `false` | Same, for WhatsApp DMs |
| `PINCER_SIGNAL_GUESTS_ALLOWED` | `false` | Same, for Signal DMs |

For Telegram and Signal, these flags are a no-op — everyone is allowed, matching today's default — if no identity map is configured at all for the deployment. WhatsApp group mentions behave the same way. WhatsApp DMs are the one exception: with no identity map configured, DMs are rejected by default regardless of `PINCER_WHATSAPP_GUESTS_ALLOWED` (matching the pre-#182 default of the removed `PINCER_WHATSAPP_DM_ALLOWLIST`, which rejected all DMs when empty) — set `PINCER_WHATSAPP_GUESTS_ALLOWED=true` to accept WhatsApp DMs from anyone even without a map. `pincer doctor` warns if a channel is configured (bot token set, or WhatsApp enabled without self-chat-only mode) but no identity map exists at all, since that means the channel has no access control whatsoever.

`PINCER_IDENTITY_MAP` + these flags are now the *only* access-control mechanism for Telegram, WhatsApp, and Signal — the older `PINCER_TELEGRAM_ALLOWED_USERS`, `PINCER_WHATSAPP_DM_ALLOWLIST`, and `PINCER_SIGNAL_ALLOWLIST` settings have been removed. Slack's equivalent (`PINCER_SLACK_USER_ALLOWLIST`) has also been removed — Slack access relies entirely on native workspace/app-install membership. Microsoft Teams keeps its own `PINCER_TEAMS_USER_ALLOWLIST`, unaffected by this change.

## API Endpoints

The API server starts automatically with `pincer run` on the configured
`PINCER_DASHBOARD_HOST:PINCER_DASHBOARD_PORT`.

| Endpoint | Auth | Description |
|----------|------|-------------|
| `GET /api/health` | No | Health check |
| `GET /api/status` | Yes | Agent and channel status |
| `GET /api/doctor` | Yes | Security doctor report |
| `GET /api/costs/today` | Yes | Today's spending + budget |
| `GET /api/costs/history?days=30` | Yes | Daily spend history |
| `GET /api/costs/by-model?days=7` | Yes | Per-model cost breakdown |
| `GET /api/costs/by-tool?days=7` | Yes | Per-tool cost breakdown |

Authentication: `Authorization: Bearer <PINCER_DASHBOARD_TOKEN>`

## Docker

```bash
# Build and run
docker compose up -d

# View logs
docker compose logs -f pincer

# Check health
curl http://localhost:8080/api/health
```

## One-Click Deploy

- **DigitalOcean**: `.do/deploy.template.yaml`
- **Railway**: `railway.toml`
- **Render**: `render.yaml`
