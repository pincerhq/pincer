# ⚙️ Configuration Reference

Pincer is configured entirely through environment variables, loaded from a `.env` file. Run `pincer init` to generate one interactively.

---

## Quick Reference

All variables use the `PINCER_` prefix.

### Core

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PINCER_LLM_PROVIDER` | Yes | `anthropic` | LLM provider: `anthropic`, `openai`, `ollama`, `openrouter`, `deepseek` |
| `PINCER_LLM_MODEL` | No | Auto | Model name (e.g., `claude-sonnet-4-20250514`, `gpt-4o`). Auto-selects best for provider. |
| `PINCER_LLM_API_KEY` | Yes* | — | API key for the LLM provider (*not needed for Ollama) |
| `PINCER_ALLOWED_USERS` | Yes | — | Comma-separated user IDs authorized to interact |
| `PINCER_DATA_DIR` | No | `./data` | Directory for database, logs, and token storage |
| `PINCER_LOG_LEVEL` | No | `INFO` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `PINCER_PERSONALITY` | No | `default` | System prompt personality: `default`, `minimal`, `professional`, or path to custom file |

### Budget & Cost

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PINCER_BUDGET_DAILY` | No | `5.00` | Max daily spend in USD |
| `PINCER_BUDGET_PER_SESSION` | No | `1.00` | Max per-conversation spend |
| `PINCER_BUDGET_PER_TOOL` | No | `0.50` | Max per-tool-call spend |
| `PINCER_BUDGET_AUTO_DOWNGRADE` | No | `true` | Auto-switch to cheaper model when budget is tight |
| `PINCER_BUDGET_WARN_PERCENT` | No | `80` | Notify user at this % of daily budget |
| `PINCER_FALLBACK_MODEL` | No | Auto | Cheaper model for budget fallback |

### Telegram

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PINCER_TELEGRAM_TOKEN` | No* | — | Bot token from @BotFather (*required if using Telegram) |
| `PINCER_TELEGRAM_WEBHOOK` | No | — | Webhook URL (if not using polling) |
| `PINCER_TELEGRAM_POLLING` | No | `true` | Use long polling (simpler, no domain needed) |

### WhatsApp

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PINCER_WHATSAPP_ENABLED` | No | `false` | Enable WhatsApp channel |
| `PINCER_WHATSAPP_DATA_DIR` | No | `data/whatsapp` | Session storage directory |

### Discord

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PINCER_DISCORD_TOKEN` | No* | — | Bot token from Developer Portal |
| `PINCER_DISCORD_GUILD_IDS` | No | — | Restrict to specific guild/server IDs |

### Email

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PINCER_EMAIL_ENABLED` | No | `false` | Enable email channel |
| `PINCER_GOOGLE_CLIENT_ID` | No | — | Google OAuth client ID |
| `PINCER_GOOGLE_CLIENT_SECRET` | No | — | Google OAuth client secret |

### Voice (Sprint 7)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PINCER_VOICE_ENABLED` | No | `false` | Enable voice calling |
| `PINCER_TWILIO_ACCOUNT_SID` | No* | — | Twilio account SID |
| `PINCER_TWILIO_AUTH_TOKEN` | No* | — | Twilio auth token |
| `PINCER_TWILIO_PHONE_NUMBER` | No* | — | Twilio phone number (E.164 format) |
| `PINCER_VOICE_STT_PROVIDER` | No | `deepgram` | Speech-to-text: `deepgram`, `whisper`, `google` |
| `PINCER_VOICE_TTS_PROVIDER` | No | `elevenlabs` | Text-to-speech: `elevenlabs`, `openai`, `google` |
| `PINCER_VOICE_TTS_VOICE_ID` | No | Auto | Voice ID for TTS provider |
| `PINCER_VOICE_MAX_CALL_DURATION` | No | `600` | Max call duration in seconds |
| `PINCER_VOICE_RECORDING_CONSENT` | No | `true` | Announce recording at call start |

### Security

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PINCER_RATE_LIMIT_USER` | No | `20` | Max messages per user per minute |
| `PINCER_RATE_LIMIT_GLOBAL` | No | `100` | Max messages globally per minute |
| `PINCER_MAX_TOOL_CALLS` | No | `10` | Max tool calls per agent turn |
| `PINCER_APPROVE_ALL_TOOLS` | No | `false` | Require approval for all tool calls |
| `PINCER_SKIP_APPROVAL` | No | — | Comma-separated tools to skip approval for |
| `PINCER_REQUIRE_SIGNED_SKILLS` | No | `false` | Only load cryptographically signed skills |
| `PINCER_AUDIT_LOG` | No | `true` | Enable structured audit logging |

### Dashboard

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PINCER_DASHBOARD_ENABLED` | No | `true` | Enable web dashboard |
| `PINCER_DASHBOARD_PORT` | No | `8080` | Dashboard port |
| `PINCER_DASHBOARD_TOKEN` | No | Auto-generated | Auth token for dashboard access |
| `PINCER_DASHBOARD_HOST` | No | `127.0.0.1` | Bind address (keep localhost for security) |

### Memory

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PINCER_MEMORY_MAX_HISTORY` | No | `50` | Max messages to keep in session buffer |
| `PINCER_MEMORY_SUMMARIZE_AFTER` | No | `100` | Auto-summarize conversations after N messages |
| `PINCER_MEMORY_SEARCH_RESULTS` | No | `5` | Number of memory search results to include in context |

### Advanced

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PINCER_SKILLS_DIR` | No | `./skills` | Directory to load custom skills from |
| `PINCER_LLM_TEMPERATURE` | No | `0.7` | LLM temperature |
| `PINCER_LLM_MAX_TOKENS` | No | `4096` | Max tokens per LLM response |
| `PINCER_STREAMING` | No | `true` | Stream LLM responses (shows typing indicator) |
| `PINCER_TIMEZONE` | No | System | Timezone for scheduled tasks (e.g., `Europe/Berlin`) |
| `PINCER_LANGUAGE` | No | `en` | Agent's primary language |

---

## Example .env File

```env
# === Core ===
PINCER_LLM_PROVIDER=anthropic
PINCER_LLM_API_KEY=sk-ant-your-key-here
PINCER_ALLOWED_USERS=123456789

# === Channels ===
PINCER_TELEGRAM_TOKEN=7000000000:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# === Budget ===
PINCER_BUDGET_DAILY=5.00
PINCER_BUDGET_AUTO_DOWNGRADE=true

# === Optional ===
# PINCER_WHATSAPP_ENABLED=true
# PINCER_DISCORD_TOKEN=your-discord-token
# PINCER_VOICE_ENABLED=true
# PINCER_TWILIO_ACCOUNT_SID=ACxxxxxxxx
# PINCER_TWILIO_AUTH_TOKEN=your-auth-token
# PINCER_TWILIO_PHONE_NUMBER=+1234567890
```

---

## Config Validation

Pincer validates all configuration on startup. If something's wrong, you'll see a clear error:

```
❌ Configuration Error:
  PINCER_LLM_API_KEY is required when using provider 'anthropic'
  PINCER_ALLOWED_USERS must contain at least one user ID
  PINCER_BUDGET_DAILY must be a positive number (got: -5)
```

Run `pincer doctor` for a comprehensive config audit.