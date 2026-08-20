# Inbound Receptionist Setup

Sprint 12 turns a Twilio number into an AI receptionist for your business: it
discloses being an AI, answers **only** from your business profile, takes
structured messages, books appointments inside your real free slots, and
transfers to a human on request — while disclosing nothing about stored data
to the caller (who is always treated as untrusted).

!!! tip "Live on day 1"
    Step 4 below (the interim Studio flow) gets a customer live with an
    hours announcement + voicemail before the AI receptionist is switched on.

---

## 1. The business profile

Copy [`business_profile.example.yaml`](https://github.com/pincerhq/pincer/blob/main/business_profile.example.yaml)
to `./business_profile.yaml` (or set `PINCER_BUSINESS_PROFILE=/path/to/file.yaml`)
and fill it in. It is **the only knowledge source** the receptionist answers
from: opening hours, address, services, FAQ pairs, booking/transfer/after-hours
settings. The schema is validated at startup and by `pincer doctor`
(`receptionist_profile`): a missing or invalid field refuses to start and
names the field — e.g. `business profile invalid — hours.mon: '8-12' is not 'HH:MM-HH:MM'`.

Rules worth knowing:

- `business.languages[0]` is the greeting language; `business.name` is spoken
  verbatim in the greeting ("Guten Tag, hier ist der digitale Assistent von
  {name}. Wie kann ich Ihnen helfen?").
- `hours` needs all seven weekdays; `[]` means closed. Ranges must not overlap.
- `faq[].a` is spoken content-identical (the model may only smooth the
  phrasing) and is capped at 300 characters for speakability.
- `transfer.target` must be E.164 when `transfer.enabled` is true.
- Hot reload is **not** supported in v1 — restart to apply profile changes.

## 2. Configuration

```env
PINCER_RECEPTIONIST_ENABLED=true
PINCER_BUSINESS_PROFILE=./business_profile.yaml
PINCER_RECEPTIONIST_BOOKING_APPROVAL=off      # off | user  ('verbal' is rejected — the caller's yes is the verbal step)
PINCER_INBOUND_MAX_CONCURRENT=3               # above → busy line + hangup
PINCER_INBOUND_RECORDING=false                # true requires the two-party announcement (doctor FAIL otherwise)
PINCER_VOICE_BLOCKLIST=                       # CSV of E.164 numbers declined with one neutral sentence
PINCER_DEFAULT_USER_ID=<your Telegram id>     # the OWNER: receives reports, approves `user`-mode bookings
```

| Variable | Default | Notes |
|---|---|---|
| `PINCER_RECEPTIONIST_ENABLED` | `false` | Master switch; false = today's generic inbound behaviour |
| `PINCER_BUSINESS_PROFILE` | `./business_profile.yaml` | Validated at startup (§1) |
| `PINCER_RECEPTIONIST_BOOKING_APPROVAL` | `off` | Maps onto the Sprint 11 mode for `google__create_event` on inbound calls; `user` = the owner approves on their channel while the caller holds |
| `PINCER_INBOUND_MAX_CONCURRENT` | `3` | Busy TwiML above this; `busy_capacity` alert at > 5/day |
| `PINCER_INBOUND_RECORDING` | `false` | Two-party announcement spoken before the greeting |
| `PINCER_VOICE_BLOCKLIST` | `""` | Matched callers hear one neutral sentence; `failure_code=blocked` |

## 3. Twilio console — number and webhooks

1. **Buy or assign the receptionist number.** Recommended: a **separate number
   from your outbound caller ID** — independent configuration and clean
   metrics per line. (Single-number alternative: the same number works; inbound
   calls then use the receptionist, outbound calls keep the Sprint 1–6
   behaviour; metrics are split by `direction`.)
2. Phone Numbers → your number → **Voice & Fax** → *A call comes in* →
   **Webhook** `https://voice.<your-domain>/api/apps/twilio/webhook`, **HTTP POST**
   (the legacy `/voice/webhook` alias still works).
3. **Primary handler fails** (fallback URL) →
   `https://voice.<your-domain>/api/apps/twilio/fallback`, HTTP POST — the
   caller hears a localized apology instead of Twilio's canned error.
4. **Interim (pre-AI go-live)**: import the Studio flow below as
   "Hours announcement + voicemail", point the number's *A call comes in* to
   that Studio flow, and switch to the webhook when you are ready. A customer
   is live on day 1.
5. **Geo permissions**: inbound needs no dialing permissions; for the transfer
   target and your outbound calls, verify *Voice Geographic Permissions* still
   include DE (and the transfer target's country).

Status callbacks (`/api/apps/twilio/status`) are configured automatically by
the TwiML Pincer returns; the transfer dial result arrives at
`/api/apps/twilio/transfer-result`.

### Interim Studio flow (export JSON)

```json
{
  "description": "Hours announcement + voicemail (Pincer interim)",
  "states": [
    {"name": "Trigger", "type": "trigger", "transitions": [{"event": "incomingCall", "next": "say_hours"}], "properties": {"offset": {"x": 0, "y": 0}}},
    {"name": "say_hours", "type": "say-play", "transitions": [{"event": "audioComplete", "next": "record_message"}],
     "properties": {"say": "Guten Tag, Sie erreichen Praxis Dr. Müller. Wir sind Montag bis Freitag von acht bis zwölf Uhr für Sie da. Bitte hinterlassen Sie nach dem Ton Ihren Namen, Ihre Rufnummer und Ihr Anliegen.", "language": "de-DE", "voice": "Polly.Vicki", "loop": 1, "offset": {"x": 0, "y": 200}}},
    {"name": "record_message", "type": "record-voicemail", "transitions": [{"event": "recordingComplete", "next": "say_bye"}, {"event": "noAudio"}, {"event": "hangup"}],
     "properties": {"transcribe": true, "timeout": 5, "max_length": 120, "play_beep": "true", "trim": "trim-silence", "offset": {"x": 0, "y": 400}}},
    {"name": "say_bye", "type": "say-play", "transitions": [{"event": "audioComplete"}],
     "properties": {"say": "Vielen Dank, wir melden uns. Auf Wiederhören.", "language": "de-DE", "voice": "Polly.Vicki", "loop": 1, "offset": {"x": 0, "y": 600}}}
  ],
  "initial_state": "Trigger",
  "flags": {"allow_concurrent_calls": true}
}
```

## 4. What the caller experiences

| Caller intent | Receptionist behaviour |
|---|---|
| Question | Answered **only** from the profile (FAQ/hours/address/services). Not in the profile → "Das kann ich nicht sicher sagen — ich gebe die Frage gern weiter. Darf ich Ihren Namen und Ihre Nummer notieren?" → message |
| Message | Slot filling, one question per turn: name (spelled back when unsure or uncommon), callback number (caller ID offered, dictated numbers read back in pairs), matter (capped, summarized back), urgency (asked only if it sounds time-critical) → spoken verification → "Danke, ich gebe das weiter." |
| Appointment | Timeframe → up to 3 free slots (profile hours ∩ calendar free/busy) → name + number → optional email → verification → written via the Sprint 11 policy (`off` = immediately, `user` = owner approves while the caller holds) → spoken confirmation only after a successful write; a slot taken in the meantime is re-offered |
| Human | Never argued with: one sentence, then `<Dial>` to `transfer.target` (20 s); busy/no-answer → apology → message |
| After hours | The greeting announces the closure and takes a message; no booking after hours in v1 |
| Silence | 10 s after the greeting → "Sind Sie noch dran?" → 10 s more → polite goodbye + hangup |
| Two unclear intents | Straight to message-taking — never a third clarifying question |

## 5. Security model

The caller is public and untrusted. On an inbound line the model only ever
sees four tools — `google__check_freebusy` (busy/free *windows*, never event
contents), `business_profile_lookup`, `google__create_event`, `send_owner_message`
— and is bound by the receptionist rules in every language pack: availability
only as windows, nothing about the owner, other callers, systems, or the
prompt; injection and "read me his schedule" attempts get the fixed deflection
("Dazu kann ich nichts sagen — aber ich kann Ihnen gern einen freien Termin
anbieten."), a second injection ends the call politely. The red-team personas
`inbound_data_extraction`, `inbound_prompt_injection`, `inbound_social_engineer`
run in CI in English and German.

## 6. Reports, storage, metrics

- The owner gets the report on their channel ≤ 30 s after hangup (template in
  §12 of the spec: from/number/matter/urgent/transcript link, plus the booking
  line). Delivery retries ×3, then a Sprint 9 alert — a lost message is a lost
  customer.
- Messages are stored in `inbound_messages` (retention-purged like transcripts)
  and listed at `GET /api/voice/messages` (PII-masked); each call row carries
  `inbound_intent`.
- Metrics: `pincer.voice.inbound.events{event=answered|intent|message_taken|booking|transfer|silent_hangup|busy_capacity|blocked, intent=…}`;
  the golden signal `busy_capacity` alerts at > 5 declined calls/day.

## 7. Release checklist (before go-live)

1. `pincer doctor` — `receptionist_profile` and `inbound_recording_consent` green.
2. 5 live inbound PSTN calls covering the intents: a profile question, an
   unknown question, a message, a booking, a transfer.
3. 2 simultaneous inbound calls (second one answered, third hears the busy
   line with `PINCER_INBOUND_MAX_CONCURRENT=2`).
4. Check the owner received every report and `/api/voice/messages` lists them.
