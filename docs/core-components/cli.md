# 🦜 Pincer CLI Reference
## Complete Command Documentation (Sprints 1–5)

---

## Installation

```bash
# From PyPI
pip install pincer-agent

# From source
git clone https://github.com/pincerhq/pincer.git && cd pincer
uv sync --all-extras

# Verify
pincer --version
```

---

## Command Overview

```
pincer
├── init                    # Interactive setup wizard
├── run                     # Start the agent
│   └── tasks               # Standalone background task worker (requires PINCER_TASK_BROKER=redis)
├── chat                    # CLI chat interface
├── config                  # Show configuration
├── cost                    # Spending summary
├── doctor                  # Security health check
├── audit                   # View audit logs
├── mcp
│   ├── list                # MCP servers + connection status
│   ├── test <server>       # Test connection to a server
│   ├── tools               # List all registered MCP tools
│   ├── call <srv> <tool>   # Call a tool directly (testing)
│   ├── status              # Live connectivity status
│   ├── search <query>      # Search MCP Registry / ClawHub
│   ├── install <pkg>       # Install MCP server from registry
│   ├── scan <pkg>          # Security scan without installing
│   ├── uninstall <name>    # Remove server from config
│   └── server              # Manage Pincer's outbound MCP endpoint
│       ├── start           # Start MCP server export
│       ├── stop            # Stop MCP server export
│       └── status          # Show status + connected clients
├── memory
│   ├── search <query>      # Search memories
│   ├── stats               # Memory usage
│   ├── clear               # Clear user memory
│   └── export              # Export to JSON
└── schedule
    └── list                # Scheduled tasks (read-only — create/remove/toggle happen via chat)
```

---

## Detailed Command Reference

---

### `pincer init`

Interactive setup wizard. Creates `.env` file with all configuration.

```bash
pincer init
```

**Flow:**
1. Choose LLM provider (Anthropic / OpenAI) → enter API key → test connection
2. Choose channels (Telegram / WhatsApp / Discord) → enter tokens → test connection
3. Set timezone, daily budget, preferences
4. Generate `.env` file

**Options:**
| Flag | Description |
|------|-------------|
| `--force` | Overwrite existing .env file |
| `--minimal` | Skip optional channels |

**Example:**
```bash
$ pincer init
🦜 Welcome to Pincer Setup!

? LLM Provider: Anthropic
? API Key: sk-ant-***
✓ Connection successful (claude-3-5-sonnet-20241022)

? Enable Telegram? Yes
? Bot Token: 123456:ABC***
✓ Bot connected: @MyPincerBot

? Daily budget ($): 5.00
? Timezone: Europe/Berlin

✓ Configuration saved to .env
  Run 'pincer run' to start your agent!
```

---

### `pincer run`

Start the Pincer agent with all configured channels.

```bash
pincer run [OPTIONS]
```

**Options:**
| Flag | Description | Default |
|------|-------------|---------|
| `--channel TEXT` | Start only this channel (telegram/whatsapp/discord) | All |
| `--reload` | Enable hot-reload for development | Off |
| `--port INT` | API server port | 8080 |
| `--host TEXT` | API server bind address | 127.0.0.1 |

**Examples:**
```bash
# Start everything
pincer run

# Telegram only
pincer run --channel telegram

# Dev mode with API on custom port
pincer run --reload --port 9090
```

**Output:**
```
🦜 Pincer Agent v0.5.0
  Provider:  anthropic (claude-3-5-sonnet-20241022)
  Channels:  telegram ✓  whatsapp ✗  discord ✓
  Budget:    $0.00 / $5.00
  API:       http://127.0.0.1:8080
  Skills:    10 loaded

✓ Agent running. Press Ctrl+C to stop.
```

**`pincer run tasks`** starts only the background task worker — no channels,
no API/MCP export, no live email/calendar polling. Requires
`PINCER_TASK_BROKER=redis` (the in-memory broker can't be shared across
processes). Used to scale schedule/webhook execution independently of the
main agent process — see [Background Tasks](background-tasks.md).

```bash
pincer run tasks
```

---

### `pincer chat`

Interactive CLI chat — test your agent without messaging apps.

```bash
pincer chat [OPTIONS]
```

**Options:**
| Flag | Description |
|------|-------------|
| `--model TEXT` | Override model (e.g., `gpt-4o-mini`) |
| `--system TEXT` | Custom system prompt |
| `--no-tools` | Disable tool use |

**Example:**
```bash
$ pincer chat
🦜 Pincer CLI Chat (type /quit to exit)

You: What's the weather in Berlin?
🔧 Using tool: openweathermap__get_current_weather("Berlin")
Pincer: Currently 3°C in Berlin with cloudy skies...

You: /quit
```

---

### `pincer config`

Show current configuration (secrets are masked).

```bash
pincer config
```

**Output:**
```
🦜 Pincer Configuration

  Provider:     anthropic
  Model:        claude-3-5-sonnet-20241022
  Anthropic:    ✓ set
  OpenAI:       ✗ not set
  Telegram:     ✓ set
  WhatsApp:     ✗ disabled
  Discord:      ✓ set
  Budget:       $5.00/day
  Data dir:     data/
  Shell:        enabled
  Log level:    INFO
```

---

### `pincer cost`

Show API spending summary.

```bash
pincer cost [OPTIONS]
```

**Options:**
| Flag | Description | Default |
|------|-------------|---------|
| `--days INT` | Show last N days | 1 (today) |
| `--by-model` | Breakdown by LLM model | Off |
| `--by-tool` | Breakdown by tool | Off |
| `--export PATH` | Export to JSON file | None |

**Examples:**
```bash
# Today's spend
pincer cost

# Last week with model breakdown
pincer cost --days 7 --by-model

# Export for accounting
pincer cost --days 30 --export costs_feb.json
```

**Output:**
```
💰 Cost Summary — Today

  Total:     $0.4231
  Requests:  47
  Budget:    $0.42 / $5.00 (8.5%)

  By Model:
    claude-3-5-sonnet   $0.3800  (42 requests)
    claude-3-5-haiku    $0.0431  (5 requests)
```

---

### `pincer doctor`

Run 36+ security health checks (including 5 MCP-specific checks).

```bash
pincer doctor [OPTIONS]
```

**Options:**
| Flag | Description |
|------|-------------|
| `--json` | Output as JSON (for CI/CD) |

**Output:**
```
🩺 Pincer Security Doctor

Running 25+ security checks...

🔑 Secrets & API Keys
  🟢 env_file_permissions: .env permissions 600 (owner only)
  🟢 api_keys_not_in_config: No API keys in config files
  🟢 gitignore_has_env: .env in .gitignore
  🔴 api_keys_not_in_git: API keys in git history!
     → Use git-filter-repo to remove

🚪 Access Control
  🟢 telegram_allowlist: Configured (1 users)
  🟡 discord_allowlist: No guild allowlist
     → Set PINCER_DISCORD_GUILD_ALLOWLIST
  🟢 dashboard_auth_token: Configured (16+ chars)

💰 Budget & Limits
  🟢 budget_limits: Daily budget: $5.00
  🟡 rate_limits: Using default (30/min)

📁 File System
  🟢 data_dir_permissions: 700
  🟢 no_world_readable_secrets: Clean

🌐 Network
  🟢 dashboard_not_exposed: Bound to 127.0.0.1
  🟢 no_debug_mode: Debug OFF

📦 Dependencies
  🟢 python_version: 3.12.8
  🟢 deps_up_to_date: Security deps current

⚙️  Runtime
  🟢 not_running_as_root: Running as: vova
  🟢 audit_logging_enabled: Enabled
  🟢 skill_sandbox_enabled: Enabled

──────────────────────────────────────────────────
Score: 88/100  🟢 20 passed  🟡 3 warnings  🔴 1 critical

⚠️  Fix critical issues before deploying!
```

---

### `pincer audit`

View security audit log entries.

```bash
pincer audit [OPTIONS]
```

**Options:**
| Flag | Description | Default |
|------|-------------|---------|
| `--limit INT` | Number of entries | 50 |
| `--action TEXT` | Filter by action type | All |
| `--user TEXT` | Filter by user ID | All |
| `--since TEXT` | From date (ISO) | All time |
| `--export PATH` | Export to JSON | None |

**Valid action types:** `tool_call`, `llm_request`, `llm_response`, `file_read`, `file_write`, `network_request`, `skill_execute`, `auth_attempt`, `config_change`, `budget_alert`, `rate_limit_hit`, `message_received`, `message_sent`, `error`

**Examples:**
```bash
# Recent entries
pincer audit

# Only tool calls from today
pincer audit --action tool_call --since 2026-02-25

# Export for compliance
pincer audit --since 2026-02-01 --export feb_audit.json
```

**Output:**
```
                     Audit Log
┌──────────────────┬──────────┬──────────────┬────────────┬──────┬────┐
│ Time             │ User     │ Action       │ Tool       │ Cost │ ms │
├──────────────────┼──────────┼──────────────┼────────────┼──────┼────┤
│ 2026-02-25T14:32 │ vova_tg  │ tool_call    │ file_write │ $0.00│ 430│
│ 2026-02-25T14:32 │ vova_tg  │ llm_request  │ sonnet     │ $0.01│ 890│
│ 2026-02-25T14:31 │ vova_tg  │ message_recv │            │      │    │
└──────────────────┴──────────┴──────────────┴────────────┴──────┴────┘

Total: 1,247 entries | Cost: $2.4510 | Failed: 3
```

---

### Skills

There is no `pincer skills` CLI command group. Skills are discovered purely from
the filesystem: drop a directory containing a `SKILL.md` file into
`src/pincer/skills/` (bundled, shipped inside the package) or `~/.pincer/skills/`
(user) and restart Pincer. See the
[Skills Guide](../guides/skills-guide.md) for the format and the
`load_skill` / `load_skill_reference` / `run_skill_script` tools the agent
uses to read and act on them.

---

### `pincer mcp list`

List all configured MCP servers and their current connection status.

```bash
pincer mcp list
```

**Output:**
```
MCP Servers (2 configured)

  Name         Transport  Status     Tools  Uptime
  ──────────────────────────────────────────────────
  github       stdio      connected  12     3m 42s
  filesystem   stdio      connected   8     3m 42s
```

---

### `pincer mcp test <server>`

Test connection to a specific MCP server. Connects, runs a ping, and reports tools discovered.

```bash
pincer mcp test github
```

**Output:**
```
Testing MCP server: github
  Transport: stdio (npx -y @modelcontextprotocol/server-github)
  ✓ Connected
  ✓ Ping OK (42ms)
  ✓ 12 tools discovered
```

---

### `pincer mcp tools`

List all tools currently registered from all connected MCP servers.

```bash
pincer mcp tools
```

**Output:**
```
MCP Tools (20 registered)

  Tool                              Server      Approval
  ─────────────────────────────────────────────────────
  github__search_repositories       github      No
  github__create_issue              github      Yes
  filesystem__read_file             filesystem  No
  filesystem__write_file            filesystem  Yes
  ...
```

---

### `pincer mcp call <server> <tool>`

Call a specific MCP tool directly, useful for testing without the agent.

```bash
pincer mcp call github search_repositories --args '{"query": "pincer language:python"}'
```

**Output:**
```
Calling github/search_repositories...
{
  "repositories": [
    {"name": "pincerhq/pincer", "stars": 847, "description": "..."}
  ]
}
```

---

### `pincer memory search <query>`

Search conversation memories using FTS5.

```bash
pincer memory search <query>
```

**Example:**
```bash
$ pincer memory search "radiology project"
Found 3 memories:

  1. [2026-02-20] User discussed pediatric radiology viewer...
  2. [2026-02-18] Cardiac imaging pipeline architecture...
  3. [2026-02-15] AI model for chest X-ray classification...
```

---

### `pincer memory list`

List memory records, newest first. Supports optional user, tag, and offset-based pagination filters.

```bash
pincer memory list [--user USER_ID] [--tags TAG1,TAG2] [--limit N] [--offset N]
```

| Flag | Default | Description |
|---|---|---|
| `--user` | all users | Filter by Pincer user ID |
| `--tags` | none | Comma-separated tag filter (OR logic: any matching tag is included) |
| `--limit` | `20` | Records per page |
| `--offset` | `0` | Skip first N records |

**Example:**
```bash
$ pincer memory list --user 123456789 --tags category:exchange --limit 5
  [exchange] [user:123456789, category:exchange] What is the weather like today?
  [exchange] [user:123456789, category:exchange] Remind me to call the dentist...
  [exchange] [user:123456789, category:exchange] Set a timer for 10 minutes

Showing 3 records (offset=0)
```

---

### `pincer memory stats`

Show memory storage statistics.

```bash
$ pincer memory stats
📊 Memory Statistics

  Conversations:  142
  Memories:       89
  Entities:       34
  Database size:  2.4 MB
  FTS5 index:     0.8 MB
```

---

### `pincer memory clear`

Clear memory for a specific user.

```bash
pincer memory clear --user USER_ID [--confirm]
```

---

### `pincer memory export`

Export user memories to JSON.

```bash
pincer memory export --user USER_ID --output memories.json
```

---

### `pincer schedule list`

List all scheduled tasks across all users (read-only, admin/debugging use).
To manage your own schedules, talk to the agent — see below.

```bash
$ pincer schedule list
             Scheduled Tasks
┏━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━┓
┃ Name          ┃ Cron       ┃ User  ┃ Timezone ┃ Enabled ┃
┡━━━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━┩
│ daily-digest  │ 0 8 * * *  │ john  │ UTC      │ yes     │
└───────────────┴────────────┴───────┴──────────┴─────────┘
```

---

### Creating, removing, and toggling schedules — via chat, not the CLI

There is no `pincer schedule add`/`remove` CLI command. Schedules are
created, removed, and enabled/disabled entirely through chat, via four
tools the agent calls on your behalf: `schedule_create`, `schedule_list`,
`schedule_remove`, `schedule_toggle`. For example, telling the bot
*"send me a digest every day at 8am"* triggers a `schedule_create` call.

Full details — guardrails, `cron_expr` vs `run_in_minutes`, per-schedule
tool allow-lists, and the standalone `pincer run tasks` worker for scaling
execution — are in [Background Tasks](background-tasks.md).

---

## Environment Variables Reference

All variables use `PINCER_` prefix.

### Required
| Variable | Description |
|----------|-------------|
| `PINCER_ANTHROPIC_API_KEY` | Anthropic API key |
| `PINCER_TELEGRAM_TOKEN` | Telegram bot token |

### LLM
| Variable | Default | Description |
|----------|---------|-------------|
| `PINCER_DEFAULT_PROVIDER` | `anthropic` | Primary provider name (`openai`/`anthropic` or a `*_COMPATIBLE_PROVIDER`) |
| `PINCER_FALLBACK_PROVIDERS` | | Up to 3 comma-separated provider names; random failover on error |
| `PINCER_DEFAULT_MODEL` | `claude-sonnet-4-5-20250929` | Model for the primary; fallback for any provider without its own `*_MODEL` |
| `PINCER_ANTHROPIC_MODEL` | | Model for the well-known `anthropic` provider |
| `PINCER_OPENAI_API_KEY` | | OpenAI API key (well-known `openai`) |
| `PINCER_OPENAI_MODEL` | | Model for the well-known `openai` provider (e.g. `gpt-4o`) |
| `PINCER_OPENAI_COMPATIBLE_PROVIDER` | | Name for an OpenAI-wire endpoint (e.g. `grok`, `ollama`) |
| `PINCER_OPENAI_COMPATIBLE_BASE_URL` | | Base URL for that endpoint |
| `PINCER_OPENAI_COMPATIBLE_API_KEY` | | Optional key for that endpoint |
| `PINCER_OPENAI_COMPATIBLE_MODEL` | | Model for that endpoint (e.g. `grok-3`, `llama3.2`) |
| `PINCER_ANTHROPIC_COMPATIBLE_PROVIDER` | | Name for an Anthropic-wire endpoint |
| `PINCER_ANTHROPIC_COMPATIBLE_BASE_URL` | | Base URL for that endpoint |
| `PINCER_ANTHROPIC_COMPATIBLE_API_KEY` | | Optional key for that endpoint |
| `PINCER_ANTHROPIC_COMPATIBLE_MODEL` | | Model for that endpoint |
| `PINCER_SUMMARY_PROVIDER` | primary | Pool provider used by the Summarizer |

### Channels
| Variable | Default | Description |
|----------|---------|-------------|
| `PINCER_TELEGRAM_ALLOWLIST` | | Comma-separated user IDs |
| `PINCER_WHATSAPP_ENABLED` | `false` | Enable WhatsApp |
| `PINCER_WHATSAPP_DM_POLICY` | `allowlist` | DM policy |
| `PINCER_DISCORD_TOKEN` | | Discord bot token |
| `PINCER_DISCORD_GUILD_ALLOWLIST` | | Comma-separated guild IDs |

### Background Tasks

See [Background Tasks](background-tasks.md) for the full architecture.

| Variable | Default | Description |
|----------|---------|-------------|
| `PINCER_TASK_BROKER` | `memory` | `memory` (single-process, zero infra) or `redis` (required for `pincer run tasks`) |
| `PINCER_TASK_BROKER_URL` | | Broker URL, e.g. `redis://localhost:6379/0` (required when `task_broker=redis`) |
| `PINCER_TASK_MAX_RETRIES` | `3` | Max attempts for a background task actor before giving up |
| `PINCER_TASK_POLL_INTERVAL` | `60` | Seconds between checks for due cron schedules |
| `PINCER_SCHEDULE_TOOL_ENABLED` | `true` | Enable the `schedule_create`/`list`/`remove`/`toggle` chat tools |
| `PINCER_MAX_SCHEDULES_PER_USER` | `20` | Max active recurring schedules per user |
| `PINCER_MIN_SCHEDULE_INTERVAL_MINUTES` | `15` | Reject schedules that would fire more often than this |

### Budget & Limits
| Variable | Default | Description |
|----------|---------|-------------|
| `PINCER_DAILY_BUDGET` | `5.0` | Daily spend limit ($) |
| `PINCER_CONV_BUDGET` | `1.0` | Per-conversation limit ($) |
| `PINCER_TOOL_BUDGET` | `0.50` | Per-tool-call limit ($) |
| `PINCER_BUDGET_WARN_PCT` | `0.80` | Warning at this % |
| `PINCER_AUTO_DOWNGRADE_PCT` | `0.70` | Auto-downgrade at this % |
| `PINCER_RATE_MESSAGES_PER_MIN` | `30` | Messages per minute |
| `PINCER_RATE_TOOLS_PER_MIN` | `20` | Tool calls per minute |
| `PINCER_MAX_CONCURRENT_LLM` | `5` | Max parallel LLM calls |
| `PINCER_MAX_DAILY_SPEND` | `5.0` | Hard daily limit ($) |

### Security
| Variable | Default | Description |
|----------|---------|-------------|
| `PINCER_DASHBOARD_TOKEN` | | API auth token (32+ chars) |
| `PINCER_DASHBOARD_HOST` | `127.0.0.1` | API bind address |
| `PINCER_DASHBOARD_PORT` | `8080` | API port |
| `PINCER_DASHBOARD_URL` | | External URL for CORS |
| `PINCER_DEBUG` | `false` | Enable debug mode |
| `PINCER_AUDIT_DISABLED` | `false` | Disable audit logging |
| `PINCER_TOOL_APPROVAL` | `auto` | Tool approval mode |
| `PINCER_SKILL_SANDBOX_DISABLED` | `false` | Disable skill sandbox |
| `PINCER_ENCRYPT_CONVERSATIONS` | `false` | Encrypt stored convos |

### Data
| Variable | Default | Description |
|----------|---------|-------------|
| `PINCER_DATA_DIR` | `data/` | Data directory path |
| `PINCER_LOG_LEVEL` | `INFO` | Log level |
