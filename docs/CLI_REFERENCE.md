# Pincer CLI Reference

Complete command reference across all sprints (1–13). For the full list of tools the agent itself can call, see [TOOLS_CATALOG.md](TOOLS_CATALOG.md) — **304 first-party tools + unlimited via MCP**.

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
| `pincer run --channel telegram` | Start only Telegram channel | 1 |
| `pincer run --channel whatsapp` | Start only WhatsApp channel | 3 |
| `pincer run --channel discord` | Start only Discord channel | 4 |
| `pincer chat` | CLI chat interface (no messaging app needed) | 4 |
| `pincer config` | Show current configuration (masked secrets) | 1 |
| `pincer pair-whatsapp` | Pair WhatsApp via QR code | 3 |
| `pincer auth-google` | Google Calendar OAuth consent flow (legacy, 3 tools) | 3 |
| `pincer setup-google` | Google Workspace OAuth consent flow (113 tools incl. Meet v2) | 9 |
| `pincer setup-ms365` | Microsoft 365 device code auth flow (69 tools) | 10 |
| `pincer signal start/stop/status/link` | Manage Signal integration (signal-cli sidecar) | 7.5 |

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

| Command | Description | Sprint |
|---------|-------------|--------|
| `pincer skills list` | Show installed skills with status | 4 |
| `pincer skills install <path>` | Install skill from path (scans first) | 4 |
| `pincer skills create <name>` | Scaffold a new skill (manifest + template) | 4 |
| `pincer skills scan <path>` | Security scan a skill directory (0-100 score) | 4 |
| `pincer skills remove <name>` | Uninstall a user skill | 5 |
| `pincer skills info <name>` | Show skill details (version, permissions, env) | 5 |

## Memory

| Command | Description | Sprint |
|---------|-------------|--------|
| `pincer memory search <query>` | Search conversation memory | 5 |
| `pincer memory stats` | Show memory usage stats | 5 |
| `pincer memory clear --user USER_ID` | Clear memory for a user | 5 |
| `pincer memory export --user USER_ID` | Export user memories to JSON | 5 |

## Proactive Agent

| Command | Description | Sprint |
|---------|-------------|--------|
| `pincer schedule list` | List all scheduled tasks | 5 |

## Google Workspace

| Command | Description | Sprint |
|---------|-------------|--------|
| `pincer setup-google` | One-time OAuth consent — opens browser, saves token to `~/.pincer/google_workspace_token.json` | 9 |

### What `pincer setup-google` does

1. Verifies `~/.pincer/google_credentials.json` (or `data/google_credentials.json`) — shows instructions if missing
2. Launches `InstalledAppFlow` — opens browser at Google consent page
3. Requests all scopes covering Gmail, Calendar, Drive, Docs, Sheets, Slides, Meet, Tasks, and Contacts/People API
4. Saves the token with `chmod 0o600`
5. Reports: `Google Workspace tools enabled (113 tools)`

After setup, the 113 `google__*` tools are auto-registered every time `pincer run` is started — no further configuration needed. Full catalog: [TOOLS_CATALOG.md](TOOLS_CATALOG.md#google-workspace-113-tools).

### Getting `google_credentials.json`

1. Open [Google Cloud Console](https://console.cloud.google.com)
2. Create/select a project → APIs & Services → Enable APIs (Gmail, Calendar, Drive, Docs, Sheets, Slides, Tasks, People)
3. Credentials → Create → OAuth 2.0 Client ID → Desktop App
4. Download JSON → save as `~/.pincer/google_credentials.json`

## Microsoft 365

| Command | Description |
|---------|-------------|
| `pincer setup-ms365` | One-time device code auth — prompts for Azure App client ID, displays device code, caches token to `~/.pincer/ms365_token_cache.json` |

### What `pincer setup-ms365` does

1. Prompts for **Application (client) ID** (from Azure App Registrations) and optional **Tenant ID** (default: `common`)
2. Starts MSAL device code flow — prints `https://microsoft.com/devicelogin` + one-time code
3. Waits while you complete sign-in in a browser
4. Caches the token at `~/.pincer/ms365_token_cache.json` (mode `0600`)
5. Appends `[integrations.ms365]` section to `pincer.toml`
6. Reports: `Microsoft 365 authenticated! … 69 Microsoft 365 tools are now available`

After setup, all 69 tools are auto-registered on every `pincer run`. Full catalog: [TOOLS_CATALOG.md](TOOLS_CATALOG.md#microsoft-365-69-tools).

## Slack Native

The Slack *channel* (bot that responds to @mentions and DMs) and the Slack *native tools* (71 tools for reading/posting messages, managing channels, users, files) are both enabled by setting `PINCER_SLACK_BOT_TOKEN`.

| Requirement | For |
|---|---|
| `PINCER_SLACK_BOT_TOKEN=xoxb-…` | All 71 Slack tools + channel |
| `PINCER_SLACK_USER_TOKEN=xoxp-…` (optional) | `slack__search_messages` and `slack__search_files` (full-text search requires a user token per Slack API) |

Full catalog: [TOOLS_CATALOG.md](TOOLS_CATALOG.md#slack-native-71-tools).

## MCP

| Command | Description |
|---------|-------------|
| `pincer mcp list` | Connected MCP servers and status |
| `pincer mcp tools` | List every MCP tool currently registered |
| `pincer mcp test <server>` | Smoke-test a specific MCP server connection |
| `pincer mcp call <server> <tool> --arg v` | Invoke a tool directly |
| `pincer mcp server start|stop|status` | Manage Pincer's outbound MCP server endpoint |
| `pincer mcp oauth init` | Generate Ed25519 signing key + seed OAuth tables |
| `pincer mcp oauth client add|list` | Register / list OAuth 2.0 clients |

See [mcp-guide.md](mcp-guide.md) for the full MCP client + OAuth 2.0 server documentation.

### Getting an Azure App client ID

1. Open [Azure App Registrations](https://portal.azure.com/#blade/Microsoft_AAD_RegisteredApps/ApplicationsListBlade)
2. **New registration** → name it, choose account type, leave redirect URI empty
3. Under **Authentication** → **Advanced settings** → enable **Allow public client flows**
4. Copy the **Application (client) ID**

See [Microsoft 365 Setup Guide](ms365-setup.md) for the full walkthrough.

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
