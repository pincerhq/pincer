# Local Tunnel (ngrok)

Some Pincer channels require a **public HTTPS URL** that an external service can call back to your machine:

- **Microsoft Teams** — Azure Bot Service posts every incoming message to a webhook URL you register in the Azure portal.
- **Voice calls (Twilio)** — Twilio posts call events to a webhook URL you configure per phone number.

When your machine is behind a firewall or a home router (no static IP, no port forwarding), these services cannot reach `localhost`. The built-in ngrok integration solves this: Pincer opens a secure tunnel automatically after the API server starts, so you get a stable public URL with zero manual networking.

---

## How it works

When `PINCER_NGROK_AUTHTOKEN` is set, the startup sequence is:

1. Channels connect (Telegram, WhatsApp, Teams, …)
2. Main API server starts on `PINCER_DASHBOARD_PORT` (default `8080`)
3. ngrok tunnel opens to that port — public URL printed to the console
4. Teams webhook and Twilio webhook both go through the same tunnel

All channel traffic shares the single API port, so one tunnel covers everything.

---

## Step 1: Get an ngrok auth token

1. Sign up for a free account at [ngrok.com](https://ngrok.com).
2. In the ngrok dashboard, go to **Your Authtoken** and copy it.
3. (Optional) In **Domains → Create a domain**, reserve a free static domain such as `my-bot.ngrok-free.app`. Without a static domain, the tunnel URL changes every restart.

---

## Step 2: Install the local extras

```bash
pip install 'pincer-agent[local]'
# or with uv:
uv pip install 'pincer-agent[local]'
```

This adds `pyngrok`, which Pincer imports lazily — it is never loaded unless `PINCER_NGROK_AUTHTOKEN` is set.

---

## Step 3: Configure `.env`

```env
# Required — activates the tunnel
PINCER_NGROK_AUTHTOKEN=your_authtoken_here

# Optional — keeps the URL stable across restarts (free ngrok plan: 1 domain)
PINCER_NGROK_DOMAIN=my-bot.ngrok-free.app
```

Start Pincer:

```bash
pincer run
```

You should see a line like:

```
✅ API server started on http://0.0.0.0:8080
✅ Ngrok tunnel: https://my-bot.ngrok-free.app
```

The HTTPS URL is your public base URL for the steps below.

---

## Scenario A: Microsoft Teams behind a firewall

Teams requires a webhook URL before it will deliver messages. Without a public IP, you must tunnel.

### Teams webhook endpoints

After the tunnel is up, Pincer exposes Teams at:

```
POST https://<tunnel-url>/api/apps/teams/api/messages
```

The legacy path `/api/messages` (on a separate port 3978) is no longer needed — everything goes through the main API.

### Setup

**1. Create the Azure Bot registration** (skip if already done)

In the [Azure portal](https://portal.azure.com):

- **App registrations → New registration** — note the **Application (client) ID** and **Directory (tenant) ID** (single-tenant) or leave tenant as `common` (multi-tenant).
- **Certificates & Secrets → New client secret** — copy the value immediately.
- **Azure Bot → Create** — choose "Use existing app registration", paste your App ID.

**2. Set the messaging endpoint**

In the Azure Bot resource → **Configuration → Messaging endpoint**, enter:

```
https://<tunnel-url>/api/apps/teams/api/messages
```

**3. Add `.env` entries**

```env
PINCER_TEAMS_APP_ID=<Application (client) ID>
PINCER_TEAMS_APP_PASSWORD=<Client secret value>

# Single-tenant only — leave blank for multi-tenant:
PINCER_TEAMS_APP_TENANT_ID=<Directory (tenant) ID>

# Ngrok tunnel (already set in Step 3 above)
PINCER_NGROK_AUTHTOKEN=your_authtoken_here
PINCER_NGROK_DOMAIN=my-bot.ngrok-free.app  # recommended
```

**4. Start Pincer and verify**

```bash
pincer run
```

In Microsoft Teams, add the bot to a chat or channel and send a message. You should see it answered by the agent.

> **Tip:** Use a static ngrok domain. If the URL changes between restarts, you must update the messaging endpoint in the Azure portal each time.

---

## Scenario B: Voice calls (Twilio) behind a firewall

Twilio dials your bot and then calls your webhook for every call event. Without a public IP, those calls never arrive.

### Voice webhook endpoints

```
POST https://<tunnel-url>/api/apps/twilio/webhook       ← inbound call handler
POST https://<tunnel-url>/api/apps/twilio/status        ← call status callbacks
WS   wss://<tunnel-url>/api/apps/twilio/relay          ← ConversationRelay WebSocket (speech in/out)
WS   wss://<tunnel-url>/api/apps/twilio/stream/{sid}    ← Media Streams (if using that engine)
```

The deprecated `/voice/*` aliases are still available but point to the same handlers — use the `/api/apps/twilio/*` paths for new configurations.

### Setup

**1. Get Twilio credentials**

Log in to [console.twilio.com](https://console.twilio.com):

- Copy your **Account SID** and **Auth Token** from the dashboard.
- Buy (or use an existing) phone number with Voice capability.

**2. Add `.env` entries**

```env
PINCER_VOICE_ENABLED=true
PINCER_TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
PINCER_TWILIO_AUTH_TOKEN=your_auth_token
PINCER_TWILIO_PHONE_NUMBER=+15551234567

# The public base URL Twilio will call — set to your tunnel URL:
PINCER_VOICE_WEBHOOK_BASE_URL=https://my-bot.ngrok-free.app

# Engine: conversation_relay (default) or media_streams
PINCER_VOICE_ENGINE=conversation_relay

# Ngrok tunnel
PINCER_NGROK_AUTHTOKEN=your_authtoken_here
PINCER_NGROK_DOMAIN=my-bot.ngrok-free.app
```

**3. Configure the Twilio phone number**

In the Twilio console, go to **Phone Numbers → Manage → Active numbers → your number → Voice Configuration**:

- **A call comes in → Webhook** → set to:
  ```
  https://my-bot.ngrok-free.app/api/apps/twilio/webhook
  ```
- **HTTP method:** POST
- **Primary handler fails → Fallback URL** (optional):
  ```
  https://my-bot.ngrok-free.app/api/apps/twilio/fallback
  ```

**4. Start Pincer and test**

```bash
pincer run
```

Call your Twilio number. The agent should pick up and respond via voice.

> **Tip:** With a static ngrok domain, the Twilio phone number configuration never needs to change between restarts.

---

## Scenario C: Teams + Voice together

Both can run simultaneously — they share the same tunnel. Add all the env vars from Scenarios A and B:

```env
# Ngrok
PINCER_NGROK_AUTHTOKEN=your_authtoken_here
PINCER_NGROK_DOMAIN=my-bot.ngrok-free.app

# Teams
PINCER_TEAMS_APP_ID=...
PINCER_TEAMS_APP_PASSWORD=...

# Voice
PINCER_VOICE_ENABLED=true
PINCER_TWILIO_ACCOUNT_SID=...
PINCER_TWILIO_AUTH_TOKEN=...
PINCER_TWILIO_PHONE_NUMBER=+15551234567
PINCER_VOICE_WEBHOOK_BASE_URL=https://my-bot.ngrok-free.app
```

One `pincer run`, one tunnel, both channels reachable.

---

## Production deployments

The tunnel is intended for **local development and testing**. For production:

- Deploy to a server with a real IP and configure HTTPS via a reverse proxy (Caddy, nginx). See [Deployment](deployment.md).
- Set `PINCER_VOICE_WEBHOOK_BASE_URL` to your production domain.
- Register the production webhook URL in the Azure portal and Twilio console.
- Remove `PINCER_NGROK_AUTHTOKEN` from the production `.env` — the tunnel will not start.

---

## Troubleshooting

### `pyngrok is not installed`

```
RuntimeError: pyngrok is not installed. Run: pip install 'pincer-agent[local]'
```

Install the local extra (Step 2 above).

### Tunnel URL keeps changing

Set `PINCER_NGROK_DOMAIN` to a static domain from your ngrok account. Free accounts include one static domain.

### Teams bot not responding

1. Confirm the tunnel is up (`Ngrok tunnel: https://...` in the console output).
2. Check the messaging endpoint in the Azure portal matches the current tunnel URL exactly, including the path `/api/apps/teams/api/messages`.
3. Run `pincer doctor` — it reports whether Teams credentials are configured.

### Twilio calls not connecting

1. Confirm `PINCER_VOICE_WEBHOOK_BASE_URL` matches the tunnel URL (no trailing slash).
2. Check the Twilio phone number webhook URL in the console.
3. Twilio requires HTTPS — ngrok provides this automatically; a plain `http://` URL will be rejected.
4. Check Twilio's **Monitor → Errors** for the specific error code and Pincer's logs for the corresponding event.
