# ⚡ Quickstart — Pincer in 5 Minutes

Get a personal AI agent running on your machine, answering you on Telegram, in under 5 minutes.

---

## Prerequisites

- **Python 3.12+** — [install](https://python.org/downloads)
- **A Telegram account** — you'll create a bot via BotFather
- **An LLM API key** — Anthropic (Claude) recommended, OpenAI and Grok also supported; or run **Ollama locally** (no API key needed)

---

## Step 1: Install Pincer

```bash
pip install pincer-agent
```

Or with `uv` (recommended, 10-100× faster):

```bash
uv pip install pincer-agent
```

Verify it worked:

```bash
pincer --version
# pincer-agent 0.7.0
```

---

## Step 2: Run the Setup Wizard

```bash
pincer init
```

The wizard walks you through:

1. **LLM Provider** — paste your Anthropic, OpenAI, or Grok API key; or choose Ollama for local models (no key needed)
2. **Telegram Bot** — open [@BotFather](https://t.me/BotFather) in Telegram, send `/newbot`, follow prompts, paste the token
3. **Allowed Users** — enter your Telegram user ID (the wizard shows you how to find it)
4. **Budget** — set a daily spending limit (default: $5/day)

This creates a `.env` file in your working directory. That's your entire config.

---

## Step 3: Start the Agent

```bash
pincer run
```

You should see:

```
🦜 Pincer v0.7.0
✅ LLM: Claude claude-sonnet-4-20250514 (Anthropic)
✅ Channel: Telegram (@your_bot_name)
✅ Memory: SQLite (data/pincer.db)
✅ Tools: 12 loaded (3 require approval)
✅ Budget: $5.00/day ($0.00 spent)
🟢 Agent is running. Send a message to your bot!
```

Open Telegram, find your bot, and send a message. That's it.

---

## Step 4: Try These Commands

Send these to your bot via Telegram to see what it can do:

| Message | What happens |
|---------|-------------|
| `What can you do?` | Lists all available tools and skills |
| `Search for the latest news about AI agents` | Web search + summarization |
| `What's on my calendar today?` | Reads Google Calendar (after OAuth setup) |
| `Schedule a meeting tomorrow at 2pm` | Creates calendar event; agent returns direct link for verification |
| `Summarize my unread emails` | Reads Gmail inbox (after OAuth setup) |
| `Remind me to call the dentist tomorrow at 9am` | Creates a scheduled reminder |
| `Run: ls -la` | Executes a shell command (asks for approval first) |
| `How much have you cost me today?` | Shows token usage and spend |

---

## Step 5: Add More Channels (Optional)

### WhatsApp

```bash
pincer channels add whatsapp
```

This opens a QR code in your terminal. Scan it with WhatsApp on your phone (Settings → Linked Devices → Link a Device). No API key needed — Pincer uses the multi-device protocol directly.

### Discord

```bash
pincer channels add discord
```

You'll need a Discord bot token from the [Discord Developer Portal](https://discord.com/developers/applications). The wizard walks you through it.

### Email

```bash
pincer channels add email
```

Connects to your Gmail via OAuth. Pincer can then read, search, draft, and send emails on your behalf.

---

## Step 6: Enable Google Workspace (Optional)

To unlock Gmail, Google Calendar, Drive, Docs, Sheets, Slides, Meet, Tasks, and Contacts — 113 tools total:

```bash
pincer setup-google
```

This opens a browser window for Google OAuth consent. Grant the requested permissions. Pincer stores the token locally at `~/.pincer/google_workspace_token.json` (readable only by you).

You'll need a `google_credentials.json` OAuth client file first — see [CLI Reference](../reference/cli.md) for how to get one from Google Cloud Console.

Required scopes (all requested in a single consent flow):
- `gmail.readonly` / `gmail.compose` / `gmail.modify` — read, draft, and send emails
- `calendar.events` — create and edit calendar events
- `calendar.readonly` — read calendar and event list
- `drive` — read and write Drive files
- `documents` — read and write Google Docs
- `spreadsheets` — read and write Google Sheets
- `presentations` — read and write Google Slides
- `tasks` — manage Google Tasks
- `contacts.readonly` / `directory.readonly` — read People / Contacts

Once configured, all 85 `google__*` tools are available every time `pincer run` starts — no further setup needed.

---

## Step 7: Enable Microsoft 365 (Optional)

To unlock Outlook email, Calendar, OneDrive, To Do, Teams, Contacts, and OneNote — 69 tools total:

```bash
pincer setup-ms365
```

You'll need to [register a free Azure app](https://portal.azure.com/#blade/Microsoft_AAD_RegisteredApps/ApplicationsListBlade) first to get a client ID. The wizard walks you through it. The token is cached locally at `~/.pincer/ms365_token_cache.json` (readable only by you).

See [Microsoft 365 Setup Guide](../guides/ms365-setup.md) for detailed instructions.

---

## What's Next?

- **[CLI Reference](../reference/cli.md)** — all commands and config options explained
- **[Skills Guide](../guides/skills-guide.md)** — build custom skills and tools *(deprecated in 0.8.0, removed in 0.9.0 — use MCP instead)*
- **[Security Model](../core-components/security-model.md)** — understand how Pincer keeps you safe
- **[Project Structure](project-structure.md)** — architecture overview and module map
- **[Deployment](deployment.md)** — run Pincer 24/7 with Docker
- **[Voice Calling](../guides/voice-calling-setup.md)** — make and receive phone calls
- **[MCP Guide](../guides/mcp-guide.md)** — connect Pincer to external tools via MCP

---

## Troubleshooting

### `pincer: command not found`

Make sure your Python scripts directory is in your PATH:

```bash
# Find where pip installed it
python -m site --user-base
# Add the bin directory to your PATH
export PATH="$HOME/.local/bin:$PATH"
```

### Telegram bot not responding

1. Check the bot token is correct in `.env`
2. Make sure your Telegram user ID is in `PINCER_ALLOWED_USERS`
3. Run `pincer doctor` to diagnose issues

### LLM errors

```bash
pincer doctor
```

This runs 25+ health checks including API key validation, network connectivity, and budget status.

### Still stuck?

- [Open an issue](https://github.com/pincerhq/pincer/issues/new?template=bug_report.md)
- [Join Discord](https://discord.gg/pincer) — we respond fast
