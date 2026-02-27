# 🦀 Pincer CLI Reference
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
├── chat                    # CLI chat interface
├── config                  # Show configuration
├── cost                    # Spending summary
├── doctor                  # Security health check
├── audit                   # View audit logs
├── skills
│   ├── list                # Installed skills
│   ├── install <url>       # Install skill
│   ├── create <name>       # Scaffold new skill
│   ├── scan <path>         # Security scan
│   ├── remove <name>       # Uninstall skill
│   └── info <name>         # Skill details
├── memory
│   ├── search <query>      # Search memories
│   ├── stats               # Memory usage
│   ├── clear               # Clear user memory
│   └── export              # Export to JSON
└── schedule
    ├── list                # Scheduled tasks
    ├── add <time> <desc>   # Add task
    └── remove <id>         # Remove task
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
🦀 Welcome to Pincer Setup!

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
🦀 Pincer Agent v0.5.0
  Provider:  anthropic (claude-3-5-sonnet-20241022)
  Channels:  telegram ✓  whatsapp ✗  discord ✓
  Budget:    $0.00 / $5.00
  API:       http://127.0.0.1:8080
  Skills:    10 loaded

✓ Agent running. Press Ctrl+C to stop.
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
🦀 Pincer CLI Chat (type /quit to exit)

You: What's the weather in Berlin?
🔧 Using tool: web_search("weather Berlin")
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
🦀 Pincer Configuration

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

Run 25+ security health checks.

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
│ 2026-02-25T14:32 │ vova_tg  │ tool_call    │ web_search │ $0.00│ 430│
│ 2026-02-25T14:32 │ vova_tg  │ llm_request  │ sonnet     │ $0.01│ 890│
│ 2026-02-25T14:31 │ vova_tg  │ message_recv │            │      │    │
└──────────────────┴──────────┴──────────────┴────────────┴──────┴────┘

Total: 1,247 entries | Cost: $2.4510 | Failed: 3
```

---

### `pincer skills list`

Show all installed skills.

```bash
pincer skills list
```

**Output:**
```
🔧 Installed Skills (10)

  Name             Version  Safety   Status
  ─────────────────────────────────────────
  weather          0.1.0    92/100   ✓ active
  news             0.1.0    88/100   ✓ active
  translate        0.1.0    95/100   ✓ active
  summarize_url    0.1.0    85/100   ✓ active
  youtube_summary  0.1.0    78/100   ✓ active
  expense_tracker  0.1.0    90/100   ✓ active
  habit_tracker    0.1.0    91/100   ✓ active
  pomodoro         0.1.0    93/100   ✓ active
  stock_price      0.1.0    82/100   ✓ active
  git_status       0.1.0    70/100   ✓ active
```

---

### `pincer skills install <url>`

Install a skill from URL or local path. Scans for security first.

```bash
pincer skills install <url_or_path>
```

**Example:**
```bash
$ pincer skills install https://github.com/user/pincer-skill-notion
🔍 Scanning skill...
  Safety score: 85/100
  Permissions: network:api.notion.com
  Env required: NOTION_API_KEY

? Install this skill? Yes
✓ Installed: notion v0.1.0
```

---

### `pincer skills create <name>`

Scaffold a new skill from template.

```bash
pincer skills create <name>
```

**Creates:**
```
~/.pincer/skills/<name>/
├── skill.py
├── manifest.json
└── README.md
```

---

### `pincer skills scan <path>`

Security scan a skill directory (static analysis).

```bash
pincer skills scan <path>
```

**Output:**
```
🔍 Scanning: ./my-skill/

  AST analysis...
  ✓ No os.system/subprocess calls
  ✓ No eval/exec usage
  🟡 Network call to undeclared domain: api.example.com
  ✓ No filesystem access outside skill dir

  Safety Score: 78/100
  Verdict: ⚠️ Review network permissions before installing
```

---

### `pincer skills remove <name>`

Uninstall a skill.

```bash
$ pincer skills remove notion
? Remove skill 'notion'? Yes
✓ Removed: notion
```

---

### `pincer skills info <name>`

Show detailed skill information.

```bash
$ pincer skills info weather
🔧 Skill: weather v0.1.0

  Author:       pincerhq
  Description:  Get weather forecasts
  Safety:       92/100
  Status:       active

  Permissions:
    network: api.openweathermap.org

  Environment:
    OPENWEATHER_API_KEY (required)

  Tools:
    get_weather(city: str) → Weather forecast
    get_forecast(city: str, days: int) → Multi-day forecast
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

List all scheduled proactive tasks.

```bash
$ pincer schedule list
⏰ Scheduled Tasks

  ID   Time    Channel    Description
  ──────────────────────────────────────
  1    08:00   telegram   Morning briefing
  2    18:00   telegram   Daily expense summary
  3    */30m   discord    Check email notifications
```

---

### `pincer schedule add`

Add a new scheduled task.

```bash
pincer schedule add <time> <description> [--channel TEXT]
```

**Examples:**
```bash
pincer schedule add "08:00" "Morning briefing" --channel telegram
pincer schedule add "*/15m" "Check for new emails"
```

---

### `pincer schedule remove`

Remove a scheduled task.

```bash
pincer schedule remove <id>
```

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
| `PINCER_DEFAULT_PROVIDER` | `anthropic` | LLM provider |
| `PINCER_DEFAULT_MODEL` | `claude-3-5-sonnet-20241022` | Model name |
| `PINCER_OPENAI_API_KEY` | | OpenAI API key |

### Channels
| Variable | Default | Description |
|----------|---------|-------------|
| `PINCER_TELEGRAM_ALLOWLIST` | | Comma-separated user IDs |
| `PINCER_WHATSAPP_ENABLED` | `false` | Enable WhatsApp |
| `PINCER_WHATSAPP_DM_POLICY` | `allowlist` | DM policy |
| `PINCER_DISCORD_TOKEN` | | Discord bot token |
| `PINCER_DISCORD_GUILD_ALLOWLIST` | | Comma-separated guild IDs |

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
