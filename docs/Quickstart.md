# ⚡ Quickstart — Pincer in 5 Minutes

Get a personal AI agent running on your machine, answering you on Telegram, in under 5 minutes.

---

## Prerequisites

- **Python 3.11+** — [install](https://python.org/downloads)
- **A Telegram account** — you'll create a bot via BotFather
- **An LLM API key** — Anthropic (Claude) recommended, OpenAI also supported

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

1. **LLM Provider** — paste your Anthropic or OpenAI API key
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
🦀 Pincer v0.7.0
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

## Step 6: Enable Google Services (Optional)

To use Gmail and Google Calendar:

```bash
pincer google setup
```

This opens a browser window for Google OAuth. Grant the requested permissions. Pincer stores tokens locally in `data/google_tokens.json`.

Required scopes:
- `gmail.readonly` — read emails
- `gmail.send` — send emails
- `calendar.readonly` — read calendar
- `calendar.events` — create/edit events

---

## What's Next?

- **[Configuration Reference](configuration.md)** — all 30+ config options explained
- **[Skills Guide](skills-guide.md)** — build custom skills and tools
- **[Security Model](security.md)** — understand how Pincer keeps you safe
- **[Architecture](architecture.md)** — how it all works under the hood
- **[Deployment](deployment.md)** — run Pincer 24/7 with Docker
- **[Voice Calling](voice-calling.md)** — make and receive phone calls (Sprint 7)

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