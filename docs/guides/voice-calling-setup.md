# Voice Calling Setup

Quick setup guide for outbound phone calls via Pincer. Text your agent "Call the dentist and reschedule" — it dials and talks.

For architecture, state machine, compliance, and full configuration, see [Voice calling](Voice%20calling.md).

---

## Prerequisites

- **Python 3.12+**
- **Twilio account** — [Sign up](https://www.twilio.com)
- **ngrok** (for local dev) — Twilio must reach your instance via HTTPS

---

## 1. Install Voice Dependencies

```bash
uv pip install 'pincer-agent[voice]'
```

Or from the project root:

```bash
uv sync --extra voice
```

Without this, you'll see: `Twilio SDK not installed. Install with: uv pip install 'pincer-agent[voice]'`

---

## 2. Get Twilio Credentials

1. Sign up at [twilio.com](https://www.twilio.com)
2. Get a phone number with voice capabilities
3. Note your **Account SID** and **Auth Token** from the console

---

## 3. Minimal .env (Outbound Only)

For text-initiated calls ("Call my dentist"), add to `.env`:

```env
PINCER_VOICE_OUTBOUND_ENABLED=true
PINCER_TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
PINCER_TWILIO_AUTH_TOKEN=your-auth-token
PINCER_TWILIO_PHONE_NUMBER=+1234567890
PINCER_VOICE_WEBHOOK_BASE_URL=https://your-ngrok-url
```

Set `PINCER_VOICE_ENABLED=true` only if you want **inbound** calls (people calling your Twilio number).

---

## 4. ngrok (Local Development)

Twilio needs a public HTTPS URL. Run:

```bash
ngrok http 8080
```

**Important:** Use port **8080** (Pincer API server), not 3000. Copy the `https://` URL to `PINCER_VOICE_WEBHOOK_BASE_URL`. No trailing spaces.

### Docker + ngrok

ngrok is optional and only starts when you use the `ngrok` profile. Add `NGROK_AUTHTOKEN` to your `.env` (get a free token at [ngrok dashboard](https://dashboard.ngrok.com/get-started/your-authtoken)). The ngrok container tunnels to `pincer:8080` and starts after Pincer is healthy.

Start with ngrok:

```bash
docker compose --profile ngrok up -d
```

Without the profile, only Pincer runs:

```bash
docker compose up -d
```

After starting with the ngrok profile, check the ngrok container logs for the public URL:

```bash
docker compose logs ngrok
```

Copy the `https://` URL to `PINCER_VOICE_WEBHOOK_BASE_URL` and restart Pincer if needed. On the free tier, the URL changes on each restart.

---

## 5. Twilio Trial Accounts

Trial accounts can only call **verified numbers**. Add target numbers in:

**Twilio Console → Phone Numbers → Verified Caller IDs**

Unverified numbers will fail with an error.

---

## 6. Verify Setup

```bash
pincer doctor
```

Checks Twilio credentials, webhook URL, and recording consent.

---

## Troubleshooting

### "Twilio SDK not installed"

Install the voice extra:

```bash
uv pip install 'pincer-agent[voice]'
```

Restart Pincer.

### Bot says "I'm placing the call" but no call is placed

The agent must **invoke the `make_phone_call` tool**. If it outputs text like `<attemptcall>...</attemptcall>` without calling the tool, no call happens.

**Check:**

1. **Logs** — `PINCER_LOG_LEVEL=DEBUG`:
   - `Tools available: [...]` — should include `make_phone_call`
   - `LLM requested tools: ['make_phone_call']` — model invoked the tool
   - `Tool call: make_phone_call(...)` — execution confirmed

2. **Approval** — On Telegram, tap **Approve** when the inline keyboard appears.

3. **Webhook URL** — Must be a public HTTPS URL. Startup warns if missing.

### Tool runs but bot says "unable to make phone calls"

Check logs for:

- `make_phone_call aborted:` — validation failed (webhook, E.164, daily limit)
- `make_phone_call result:` — error returned to the LLM
- `make_phone_call failed:` — Twilio API exception

**Common causes:**

- **Trial account** — Add target number to Verified Caller IDs
- **ngrok** — Ensure it's running and URL matches. Test: `curl -X POST https://your-ngrok-url/voice/relay-webhook -H "Content-Type: application/json" -d '{}'` → should return 200
- **Twilio Debugger** — Console → Monitor → Logs for webhook/API errors

---

## Next Steps

- [Voice calling](Voice%20calling.md) — Full guide: architecture, state machine, compliance, configuration reference
- [Configuration](PROJECT_STRUCTURE.md) — All env vars
