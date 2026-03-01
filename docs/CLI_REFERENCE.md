# Pincer CLI Reference

Complete command reference across all sprints (1-5).

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
| `pincer auth-google` | Google Calendar OAuth consent flow | 3 |

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
