# Voice Calling Setup

Quick setup guide for outbound phone calls via Pincer. Text your agent "Call the dentist and reschedule" — it dials and talks.

For architecture, state machine, compliance, and full configuration, see the [Voice Calling component docs](../core-components/voice-calling.md).

---

## Prerequisites

- **Python 3.12+**
- **Twilio account** — [Sign up](https://www.twilio.com)
- **ngrok** (for local or dev) — Twilio must reach your instance via HTTPS

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
PINCER_VOICE_WEBHOOK_BASE_URL=https://your-ngrok-url # optional if PINCER_BASE_URL is set
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

Checks Twilio credentials, webhook URL, recording consent, and that configured ElevenLabs voice IDs actually exist in your account.

---

## 7. ElevenLabs Voices (Optional)

By default, calls use a robotic Google voice. With an ElevenLabs voice configured, **both engines** speak with it — no code changes:

```env
PINCER_ELEVENLABS_API_KEY=your-elevenlabs-key
PINCER_ELEVENLABS_VOICE_ID=JBFqnCBsd6RMkjVDRZzb        # global
PINCER_ELEVENLABS_VOICE_ID_DE=de-tuned-voice-id        # optional, German calls
PINCER_ELEVENLABS_VOICE_ID_EN=en-tuned-voice-id        # optional, English calls
```

Voice resolution order: per-language setting → global setting → default ("Rachel"). A German call gets the German-tuned voice on either engine.

Find and audition voices:

```bash
pincer voice list                      # all voices in your account (incl. clones) with IDs
pincer voice test --language de        # sample to ~/.pincer/voice_test.wav + 8kHz mu-law variant
```

Judge the **mu-law file** — at telephony quality some voices sound very different than the ElevenLabs web preview.

### Engine specifics

- **`conversation_relay`** (default engine): Pincer sets `ttsProvider="ElevenLabs"` and your voice ID in the TwiML. Twilio synthesizes using its ElevenLabs integration, so this works with voices from the **public ElevenLabs voice library** without an ElevenLabs API key. One-time Console step: accept the AI/ML terms under **Console → Voice → Settings → Predictive and Generative AI/ML Features Addendum**. As of this writing, Twilio does not document a way to bring your own ElevenLabs API key to ConversationRelay — **custom/cloned voices from your own account need the `media_streams` engine**. Force a provider with `PINCER_CR_TTS_PROVIDER=google|amazon|elevenlabs` (empty = auto).
- **`media_streams`**: Pincer talks to ElevenLabs directly with your API key — any voice in your account works, including Professional/Instant clones. Audio is requested as native `ulaw_8000` telephony format (lower latency than the old PCM resample path). Tune with `PINCER_ELEVENLABS_MODEL` (default `eleven_flash_v2_5`, multilingual + low latency), `PINCER_ELEVENLABS_STABILITY`, `_SIMILARITY`, `_SPEED`, `_STYLE`.

A misconfigured voice ID fails at startup (`media_streams`) or falls back to the Google voice with a warning (`conversation_relay`) — never mid-call. If ElevenLabs goes down mid-call, the utterance is retried once, then the agent apologizes via Twilio's own voice and ends the call cleanly.

**Cost:** ElevenLabs Flash is ~$0.05 per 1,000 characters — roughly $0.05–0.10 of TTS on a 3-minute call, on top of Twilio. Per-call character counts are logged in the call metrics.

Cloning your own voice? See [Custom Voices](custom-voices.md) — including the consent requirements that apply (especially in DACH).

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
- **ngrok** — Ensure it's running and URL matches. Test: `curl -X POST https://your-ngrok-url/api/apps/twilio/status -d 'CallSid=test'` → should return 200

### Call connects, greeting plays, then "an application error has occurred"

That message is Twilio's own error prompt: the `<Say>` greeting worked, but the next TwiML verb failed at execution time. Check **Twilio Console → Monitor → Logs → Errors** for the exact code. The classic one is **64101** ("Invalid url parameter value in TwiML") — ConversationRelay's `url` must be a `wss://` WebSocket URL (`wss://<host>/api/apps/twilio/relay`), never `https://`. Pincer generates this correctly; if you overrode webhook wiring manually, fix the scheme. Outbound calls also register `/api/apps/twilio/fallback`, so TwiML failures play Pincer's own apology and log the `ErrorCode` — for inbound, set the same URL as the number's **Fallback URL** in the Console.
- **Twilio Debugger** — Console → Monitor → Logs for webhook/API errors

---

## DACH deployment (Germany / Austria / Switzerland)

Running Pincer's voice stack against German (+49), Austrian (+43), or Swiss (+41) numbers needs extra setup — both regulatory (number provisioning) and legal (consent, GDPR). See [DACH Compliance](dach-compliance.md) for the full data-flow documentation.

### Provisioning a German (+49) number

1. **Start early** — German numbers require an approved **regulatory bundle** (identity + local address verification). Approval by the Bundesnetzagentur-mandated process typically takes **2–5 business days**, sometimes longer.
2. In the Twilio Console: **Phone Numbers → Regulatory Compliance → Bundles → Create a Bundle**, choose *Germany*, number type *National* or *Mobile*, and the end-user type (business bundles need a trade register extract / Handelsregisterauszug).
3. Create a matching **Address** (a German service address is required for national numbers).
4. Once the bundle is approved, buy the +49 number under **Phone Numbers → Buy a Number** (filter: Germany) and attach the bundle + address.
5. Assign your voice webhook (`PINCER_VOICE_WEBHOOK_BASE_URL`) to the number as usual.

### Twilio EU (Ireland) region

Twilio processes voice traffic in the US by default. For EU data residency, use the **Ireland (IE1)** region where available:

- In the Console, switch the region selector to *Ireland (IE1)* and create API keys **in that region** — IE1 credentials are separate from US1.
- Point SDK/API calls at the regional endpoint (`https://api.dublin.ie1.twilio.com`).
- Note: not all Twilio products are available in IE1 (check current product coverage; ConversationRelay availability may lag US1). Where a product is US1-only, document that flow in your DPA/AVV instead.

### Recommended DE settings

```bash
PINCER_VOICE_CONSENT_MODE=two_party        # §201 StGB: all-party consent
PINCER_VOICE_CONSENT_LANGUAGE=de           # German consent/AI-disclosure announcement
PINCER_VOICE_LANGUAGE=de-DE
PINCER_VOICE_TIMEZONE=Europe/Berlin
PINCER_VOICE_TRANSCRIPT_RETENTION_DAYS=90  # GDPR storage limitation
```

### Verify

1. Place an outbound call from the +49 number to a German mobile: it should connect and display the +49 caller ID.
2. With `two_party` consent + `de`, the German announcement plays before the conversation starts.
3. `pincer doctor` shows the DACH voice checks (`voice_dach_consent`, `voice_retention`, `voice_provider_regions`) as green.

---

## Next Steps

- [Voice Calling](../core-components/voice-calling.md) — Full guide: architecture, state machine, compliance, configuration reference
- [Project Structure](../getting-started/project-structure.md) — All env vars and architecture overview
