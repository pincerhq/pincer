# DACH Compliance (Voice)

> **Engineering documentation, not legal advice.** This page documents how Pincer's voice stack handles audio, transcripts, and personal data so that you (or your counsel/DPO) can assess a DACH deployment. Consult a lawyer for your specific use case.

## Why this matters

Voice calls in Germany, Austria, and Switzerland produce recordings and transcripts of third parties:

- **Germany** — §201 StGB criminalizes recording the non-public spoken word without consent of **all** parties (two-party/all-party consent).
- **Switzerland** — Art. 179ter StGB likewise requires all-party consent.
- **Austria** — recording a conversation you participate in is generally permitted (one-party), but distribution is restricted; AI disclosure remains best practice.
- **GDPR** applies to transcripts and call metadata everywhere in the EU/EEA: lawful basis, transparency (Art. 13/14), and storage limitation (Art. 5(1)(e)).
- **EU AI Act** (Art. 50) — people must be informed when they are interacting with an AI system. Pincer plays an AI-disclosure announcement even when recording is off.

## Data flows

| Provider | Purpose | What it receives | Default processing region | EU option |
|---|---|---|---|---|
| **Twilio** | Telephony (PSTN), call control, optional recording | Caller/callee numbers, signaling, full call audio; recordings if enabled | US (US1) | Ireland (IE1) region — separate API keys and endpoint (`api.dublin.ie1.twilio.com`); product coverage is narrower than US1 |
| **Deepgram** | Streaming STT (media_streams engine) | Live call audio, STT language setting | US | EU-hosted endpoint available on some plans — confirm with Deepgram and configure accordingly |
| **ElevenLabs** | Streaming TTS (media_streams engine) | Agent reply text (may contain call context/PII) | US | No general EU residency at time of writing — cover via SCCs in the DPA |
| **LLM provider** (Anthropic/OpenAI/…) | Conversation reasoning | Transcribed caller utterances, tool results | Provider-dependent | Provider-dependent (check your provider's data-residency options) |
| **Google (via Twilio ConversationRelay)** | STT/TTS in the conversation_relay engine | Live call audio / reply text | Per Twilio's sub-processor terms | Covered by the Twilio DPA |

### What Pincer stores locally

| Data | Where | Retention |
|---|---|---|
| Call metadata (`voice_calls`) | SQLite (`PINCER_DB_PATH`) | `PINCER_VOICE_TRANSCRIPT_RETENTION_DAYS` (default 90; `0` = forever) |
| Utterance transcripts (`call_transcripts`) | SQLite | same |
| Tool/action log (`call_actions`) | SQLite | same |
| Audit log (every action, incl. purges) | `data/audit.db` | not auto-purged (compliance log) |

The retention purge runs daily via the built-in scheduler (03:30, in `PINCER_VOICE_TIMEZONE`/`PINCER_TIMEZONE`). Every purge that deletes rows writes an audit event of type `retention_purge` with per-table counts.

## Consent behavior

- `PINCER_VOICE_CONSENT_MODE=two_party` — an announcement plays **before** the conversation starts; continuing the call constitutes consent to recording.
- Jurisdiction auto-detection: numbers with +49 (DE) or +41 (CH) prefixes are treated as two-party even if the configured mode is `one_party` (`auto` behavior); US two-party states are detected by area code.
- `PINCER_VOICE_CONSENT_LANGUAGE=de` renders the announcement in German:
  - Recording: *"Dieser Anruf wird möglicherweise aufgezeichnet. Wenn Sie das Gespräch fortsetzen, stimmen Sie der Aufzeichnung zu."*
  - No recording (AI disclosure): *"Bitte beachten Sie: Sie sprechen mit einem automatischen KI-Assistenten."*
- With recording **disabled**, the AI-disclosure announcement still plays (unless consent mode is `none`).

### Call introduction

Before the consent announcement, the assistant introduces itself — by default *"This is Pincer, the AI assistant from 3days.ai."* (German: *"Hier spricht Pincer, der KI-Assistent von 3days.ai."*). Configure with:

- `PINCER_VOICE_ASSISTANT_NAME` (default `Pincer`; empty = skip the introduction)
- `PINCER_VOICE_ASSISTANT_ORG` (default `3days.ai`; empty = omitted)
- `PINCER_VOICE_ASSISTANT_OWNER` — adds *"and personal AI assistant of <owner>"* (empty by default)
- `PINCER_VOICE_INTRO_TEXT` — verbatim override of the whole sentence

The introduction plays regardless of consent mode and satisfies the AI-disclosure requirement on its own, so the separate disclosure line is skipped when recording is off and an introduction is configured.

## What an SMB customer needs for their DPA / AVV

A German/Austrian SMB deploying Pincer voice acts as **controller**; the providers above are **processors** (Auftragsverarbeiter). Checklist:

1. **Twilio DPA** — sign Twilio's Data Protection Addendum; if using IE1, note it in the processing-region annex. List Twilio's sub-processors (incl. Google for ConversationRelay STT/TTS).
2. **Deepgram / ElevenLabs DPAs** — only needed with the `media_streams` engine. Both are US processors; transfers rely on SCCs / EU-U.S. DPF — record this in the transfer-impact assessment.
3. **LLM provider DPA** — cover transcribed call content sent to the LLM.
4. **Records of processing (Art. 30 VZ/ROPA)** — add an entry for voice calling: categories (call audio, transcripts, phone numbers), purposes, retention (`PINCER_VOICE_TRANSCRIPT_RETENTION_DAYS`), recipients (table above).
5. **Privacy notice** — callers must be informed (Art. 13): the consent announcement covers recording; link the full notice on your website/IVR.
6. **Retention** — keep the default 90 days unless you have a documented reason; `0` (keep forever) conflicts with storage limitation and triggers a `pincer doctor` warning when recording is enabled.
7. **Data subject requests** — transcripts are plain SQLite rows keyed by call SID and phone number; export with any SQLite client, delete by `DELETE FROM call_transcripts WHERE call_id = …` (deletions land in the audit log).

## Live listen-in (monitoring)

> Engineering documentation — **flag for legal review** before enabling for a
> customer. Monitoring is legally distinct from recording, but in DACH it sits
> next to §201 StGB (confidentiality of the spoken word) and, for employees,
> BDSG / works-council co-determination (§87 BetrVG).

`PINCER_LISTEN_IN_ENABLED=true` lets the owner listen to an active call live
from the dashboard. What it is and is not:

- **Listen-only, by construction.** The audio comes from a separate Twilio
  `<Start><Stream>` fork that cannot carry audio back into the call; nobody can
  speak, whisper or barge in through it.
- **No audio is stored.** Frames are relayed from Twilio to the browser and
  never written to disk or the database (`test_no_audio_persisted`). This path
  does **not** create a recording; the recording controls
  (`PINCER_VOICE_RECORDING_ENABLED`, consent mode) are unchanged.
- **Announced.** With `PINCER_LISTEN_IN_ANNOUNCE=true` (default) the call
  opening is extended per language:
  - DE: *"Dieses Gespräch kann zur Qualitätssicherung mitgehört werden."*
  - EN: *"This call may be monitored for quality assurance."*
  - UK: *"Ця розмова може прослуховуватися для контролю якості."*

  It plays after the recording / AI-disclosure line (and before the
  receptionist greeting on inbound calls). Setting `PINCER_LISTEN_IN_ANNOUNCE=false`
  while listen-in is enabled is a `pincer doctor` **FAIL** (check
  `listen_in_announce`) unless `PINCER_VOICE_CONSENT_MODE=two_party` with
  `PINCER_VOICE_RECORDING_ENABLED=true` — i.e. the two-party recording
  announcement is already playing and covers monitoring.
- **Audited.** Every listen session is one `listen_in_session` row in the
  audit log: `{user, call_sid, started_at, ended_at, duration_s}` (plus
  frame counts and the end reason). Include it in the Art. 30 record and in
  the access-control section of the TOMs; it answers "who listened to which
  call, when, for how long".
- **Capped and authenticated.** At most `PINCER_LISTEN_IN_MAX_LISTENERS`
  (default 2) listeners per call; the listener socket requires the dashboard
  bearer token and is refused before the handshake completes without it.

For the DPA / AVV: no new processor is involved (Twilio already carries the
call audio); add *live monitoring by the controller's own staff, announced to
the caller, not recorded* to the processing description, and — where employees
are on the line — clear it with the works council first.

## Related configuration

| Variable | Default | Purpose |
|---|---|---|
| `PINCER_VOICE_CONSENT_MODE` | `one_party` | Set `two_party` for DE/CH |
| `PINCER_VOICE_CONSENT_LANGUAGE` | follows call language | `de` for German announcements |
| `PINCER_VOICE_TRANSCRIPT_RETENTION_DAYS` | `90` | Auto-purge window; `0` = keep forever |
| `PINCER_VOICE_TIMEZONE` | `PINCER_TIMEZONE` (default `Europe/Berlin`) | Voice-facing time rendering |
| `PINCER_VOICE_RECORDING_ENABLED` | `false` | Keep off unless recording is actually needed |
| `PINCER_LISTEN_IN_ENABLED` | `false` | Live listen-in media fork (listen-only, never recorded) |
| `PINCER_LISTEN_IN_ANNOUNCE` | `true` | Monitoring notice in the call opening; `false` is a doctor FAIL without two-party recording |

`pincer doctor` runs the listen-in announce gate (`listen_in_announce`, FAIL) and three DACH checks: outbound-from-DACH-number with `one_party` consent (warns), recording without a retention window (warns), and an inventory of configured STT/TTS providers and their processing regions.

## Customer-facing AVV annex

The sub-processor list, technical and organisational measures (Art. 32), and
data-subject-rights procedures are maintained as a separate, customer-facing
draft: **[Annex: Sub-processors & Technical Measures](avv-annex.md)**. It is
engineering input for legal review — it must be reviewed and adopted by counsel
before it goes to a customer.

Sprint 8 added the security controls that annex describes (webhook and
WebSocket authentication, outbound abuse limits including the do-not-call list,
in-call tool restriction, and PII masking on log egress); the control-by-control
verification record is the
[Voice Security Checklist](security-checklist.md).

See also: [Voice Calling Setup — DACH deployment](voice-calling-setup.md#dach-deployment-germany--austria--switzerland).
