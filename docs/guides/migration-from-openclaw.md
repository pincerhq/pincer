# 🔄 Migrating from OpenClaw

Already using OpenClaw? Here's how to switch to Pincer in under 30 minutes. We'll transfer your memories, convert your skills, and get you running on the same channels.

---

## Why Migrate?

| Concern | OpenClaw | Pincer |
|---------|----------|--------|
| Security | CVE-2026-25253, 341 malicious skills | Sandbox isolation, skill scanning, signing |
| Cost | Users got surprise $750 bills | Hard budget limits, auto-downgrade |
| Codebase | 200K+ LOC TypeScript | <8K LOC Python, readable in an afternoon |
| Setup | 30-60 minutes | Under 5 minutes |
| Dependencies | Node.js + npm ecosystem | Python — pip install and go |

---

## Step 1: Export Your OpenClaw Data

### Export Memories

```bash
# In your OpenClaw directory
cd ~/.openclaw

# Export conversation history
sqlite3 openclaw.db ".mode json" ".output memories.json" \
  "SELECT * FROM memories ORDER BY created_at;"

# Export entities
sqlite3 openclaw.db ".mode json" ".output entities.json" \
  "SELECT * FROM entities ORDER BY created_at;"
```

### Export Skills List

```bash
# List installed skills
openclaw skills list --json > skills-list.json
```

---

## Step 2: Install Pincer

```bash
pip install pincer-agent
pincer init
```

Follow the wizard — use the same API keys and bot tokens you had in OpenClaw.

---

## Step 3: Import Memories

```bash
pincer import openclaw --memories memories.json --entities entities.json
```

This converts OpenClaw's memory format to Pincer's schema and imports it into your new database. Conversation history, entities (people, places, projects), and summaries are all preserved.

---

## Step 4: Convert Skills

Most OpenClaw skills are TypeScript. Pincer skills are Python. For simple skills, the conversion is straightforward:

### OpenClaw Skill (TypeScript)

```typescript
import { Tool } from "@openclaw/sdk";

export const weatherTool: Tool = {
  name: "get_weather",
  description: "Get current weather for a city",
  parameters: {
    type: "object",
    properties: {
      city: { type: "string", description: "City name" },
    },
    required: ["city"],
  },
  handler: async ({ city }) => {
    const res = await fetch(`https://wttr.in/${city}?format=j1`);
    const data = await res.json();
    return `Weather in ${city}: ${data.current_condition[0].temp_C}°C`;
  },
};
```

### Pincer Equivalent (Python)

```python
from pincer.tools import tool
import httpx

@tool(
    name="get_weather",
    description="Get current weather for a city",
)
async def get_weather(city: str) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"https://wttr.in/{city}?format=j1")
        data = resp.json()
        return f"Weather in {city}: {data['current_condition'][0]['temp_C']}°C"
```

The key differences: Python instead of TypeScript, `@tool` decorator instead of object literal, `httpx` instead of `fetch`, and type hints instead of JSON Schema (Pincer generates the schema automatically).

---

## Step 5: Channel Migration

### Telegram

Use the same bot token. Just update `PINCER_TELEGRAM_TOKEN` in your `.env`:

```env
PINCER_TELEGRAM_TOKEN=7000000000:AAxxxxxxxxxx
```

Stop OpenClaw first, then start Pincer. Only one service can poll the same bot token.

### WhatsApp

WhatsApp sessions can't be transferred — you'll need to scan a new QR code:

```bash
pincer channels add whatsapp
# Scan the QR code with your phone
```

Your WhatsApp contacts won't notice any change — same phone number, different backend.

### Discord

Use the same bot token:

```env
PINCER_DISCORD_TOKEN=your-discord-token
```

---

## Step 6: Verify

```bash
# Run health checks
pincer doctor

# Verify memories imported
pincer memory stats
# Expected: X conversations, Y entities imported

# Send a test message to your bot
# Ask: "What do you remember about me?"
```

---

## Configuration Mapping

| OpenClaw Config | Pincer Equivalent |
|----------------|-------------------|
| `OPENCLAW_API_KEY` | `PINCER_LLM_API_KEY` |
| `OPENCLAW_MODEL` | `PINCER_LLM_MODEL` |
| `OPENCLAW_TELEGRAM_TOKEN` | `PINCER_TELEGRAM_TOKEN` |
| `OPENCLAW_ALLOWED_USERS` | `PINCER_ALLOWED_USERS` |
| `OPENCLAW_MAX_BUDGET` | `PINCER_BUDGET_DAILY` |
| `OPENCLAW_SKILLS_DIR` | `PINCER_SKILLS_DIR` |

---

## FAQ

**Q: Will I lose my conversation history?**
No — the import tool transfers all memories and entities.

**Q: Can I run both simultaneously?**
Not on the same bot tokens. You can run them on different Telegram bots or different WhatsApp numbers side-by-side for testing.

**Q: What about OpenClaw skills from the marketplace?**
We recommend only migrating skills you trust. Many OpenClaw marketplace skills had security issues. Pincer's skill scanner will flag any problems.

**Q: Is the memory format compatible?**
Not directly — that's what the import tool handles. It maps OpenClaw's schema to Pincer's.

---

## Need Help?

- [Open an issue](https://github.com/pincerhq/pincer/issues/new?template=bug_report.md) with the `migration` label
- [Join our Discord](https://discord.gg/pincer) — #migration channel