# Annex: Sub-processors & Technical Measures (Voice)

> **⚠️ ENGINEERING INPUT FOR LEGAL REVIEW — NOT A LEGAL DOCUMENT AND NOT LEGAL ADVICE.**
>
> This annex is drafted by engineering so that counsel or a DPO has accurate,
> current facts about what the Pincer voice stack does with personal data. It is
> a **draft input** to a customer's Auftragsverarbeitungsvertrag (AVV) / Data
> Processing Agreement, not the agreement itself. Nothing here has been reviewed
> by a lawyer. Before it is put in front of a customer it must be reviewed,
> edited, and adopted by counsel, and every `<placeholder>` filled in.
>
> Sub-processor lists and processing regions change. Verify each row against the
> provider's own current sub-processor page before signing anything.
>
> Facts last verified against the codebase: **2026-08-20** (Sprint 8).
> Review-by: **2026-11-20**, or on any provider change.

---

## A. Parties and roles

| Role | Party | Notes |
|---|---|---|
| Controller (Verantwortlicher) | `<Customer legal entity>` | Decides who is called and why |
| Processor (Auftragsverarbeiter) | `<Operator legal entity>` | Operates the Pincer instance for the customer |
| Sub-processors | See §C | Engaged by the processor to deliver telephony, speech, and inference |

For a **self-hosted** deployment where the customer runs the instance themselves,
the customer is controller *and* operator; the parties in §C are then the
customer's own direct processors and the customer contracts with each one.

## B. Subject matter of the processing

| Item | Detail |
|---|---|
| Purpose | Placing and receiving telephone calls on the controller's behalf (appointment scheduling, confirmations, enquiries) |
| Nature | Real-time speech-to-text, LLM reasoning, text-to-speech, transcript storage |
| Duration | For the term of the service agreement; per-record retention per §E |
| Categories of data subjects | The controller's own users; **third-party callees** who are not the operator's customers |
| Categories of personal data | Phone numbers (E.164), call audio in transit, utterance transcripts, call metadata (time, duration, direction, outcome), any personal data the parties themselves speak during a call |
| Special categories (Art. 9) | Not intentionally processed. May incidentally occur in speech (e.g. a health reason given when rescheduling a medical appointment). Mitigations in §F. |

## C. Sub-processor list

Complete this table against each provider's current sub-processor page before
the annex is signed; the "processing region" column is what the controller's
transfer-impact assessment depends on.

| # | Sub-processor | Function | Personal data received | Processing region (default) | EU option | Transfer mechanism |
|---|---|---|---|---|---|---|
| 1 | **Twilio Inc.** | PSTN telephony, call control, ConversationRelay | Caller/callee numbers, signalling, full call audio | US (US1) | Ireland (IE1) — separate credentials and endpoint; narrower product coverage | SCCs / EU-U.S. DPF — verify Twilio's current status |
| 2 | **Google LLC** (via Twilio ConversationRelay) | Streaming STT and TTS inside ConversationRelay | Live call audio, reply text | Per Twilio's sub-processor terms | Covered by the Twilio DPA | Flows down through Twilio's DPA |
| 3 | **Deepgram Inc.** | Streaming STT — **only** with `PINCER_VOICE_ENGINE=media_streams` | Live call audio | US | EU endpoint on some plans — confirm with Deepgram | SCCs |
| 4 | **ElevenLabs Inc.** | Streaming TTS — used by `media_streams`, and by ConversationRelay when an ElevenLabs voice is configured | Agent reply text, which may contain call context and PII | US | No general EU residency at time of writing | SCCs |
| 5 | **LLM provider** — `<Anthropic PBC / OpenAI, L.L.C. / …>` | Conversation reasoning and post-call outcome extraction | Transcribed utterances, tool results, call purpose | Provider-dependent | Provider-dependent | SCCs / provider DPA |
| 6 | **Hosting provider** — `<e.g. Hetzner Online GmbH, DE>` | Compute, storage, and network for the Pincer instance and its SQLite database | All locally stored data in §E | `<region>` | `<region>` | Intra-EU if EU-hosted; otherwise SCCs |
| 7 | **Backup storage** — `<S3-compatible provider or "none — local only">` | Encrypted database backups (`scripts/backup-db.sh`) | All locally stored data in §E, encrypted at rest | `<region>` | `<region>` | `<mechanism>` |

**Engine matters.** With `PINCER_VOICE_ENGINE=conversation_relay` (the
production default), rows 3 and 4 drop out unless an ElevenLabs voice is
configured; speech processing stays inside Twilio's stack. With
`media_streams`, rows 3 and 4 are active. Record which engine the deployment
actually runs.

## D. Data flow, in order

1. Twilio receives or places the call over the PSTN. Call audio and both phone
   numbers are in Twilio's hands from this point.
2. Twilio opens an authenticated WebSocket to the Pincer instance
   (`X-Twilio-Signature` on HTTP routes; a short-lived HMAC token on the
   WebSocket upgrade — see §F.1).
3. Speech is transcribed (Google via ConversationRelay, or Deepgram).
4. Transcribed text plus call context is sent to the LLM provider; the reply is
   synthesised to speech and played to the callee.
5. Utterances and tool actions are written to the local SQLite database with PII
   masking applied (§F.3).
6. After the call, the LLM extracts a structured outcome; the initiating user
   receives a report on their own channel.

Raw call audio is **never** stored by Pincer. Recording is a Twilio-side feature
and is off by default (`PINCER_VOICE_RECORDING_ENABLED=false`).

## E. Storage and retention (Art. 5(1)(e))

| Data | Location | Retention |
|---|---|---|
| Call metadata (`voice_calls`) | SQLite on the instance | `PINCER_VOICE_TRANSCRIPT_RETENTION_DAYS`, default **90 days** |
| Utterance transcripts (`call_transcripts`) | SQLite | same |
| Tool/action log (`call_actions`) | SQLite | same |
| Do-not-call list (`do_not_call`) | SQLite | **Retained indefinitely by design** — it is the record of an objection under Art. 21 and deleting it would defeat its purpose |
| Outbound call log (`outbound_call_log`) | SQLite | Retained for the abuse limits (daily cap, target cooldown); covered by the same purge window |
| Audit log (`data/audit.db`) | SQLite | Not auto-purged — it is the compliance record, including of the purges themselves |

The purge runs daily at 03:30 in `PINCER_VOICE_TIMEZONE` and writes a
`retention_purge` audit event with per-table row counts. `pincer doctor
--production` refuses a deployment with retention disabled.

## F. Technical and organisational measures (Art. 32)

### F.1 Authentication of every inbound surface

| Surface | Control |
|---|---|
| `/api/apps/twilio/*` and legacy `/voice/*` HTTP routes | `X-Twilio-Signature` HMAC-SHA1 validated on every request against the Twilio auth token; unsigned or forged requests get 403 and an audit entry |
| ConversationRelay / Media Streams WebSocket upgrades | Short-lived HMAC token minted into the TwiML URL; verified **before** `accept()`, so an unauthenticated socket never reaches the call |
| Replay | Timestamped requests older than `PINCER_VOICE_SIGNATURE_MAX_AGE_S` (default 300 s) are rejected even with a valid signature |
| Dashboard / REST API | Bearer token, plus per-IP exponential lockout after repeated failures; every rejection audit-logged with its IP |
| CORS | In production, only the configured real origins — no localhost |

### F.2 Abuse prevention (protects callees, §7 UWG)

All enforced server-side at a single gate that every channel crosses, so no
initiating surface can route around them:

- **Do-not-call list** — a callee who asks not to be called again is added
  automatically from the call transcript (German, English, and Ukrainian
  phrasings) and is then blocked for *every* user of the instance. Removal is a
  deliberate human action, logged.
- **Quiet hours** — no outbound calls 20:00–08:00 local by default.
- **Daily cap** — a hard ceiling on outbound calls per day across all users.
- **Per-target cooldown** — bounds how often one number can be dialled; retries
  consume the same budget, so a retry loop cannot become a robocall.

### F.3 Data minimisation on the call itself

- The agent's in-call tool set is a minimal allowlist with a hard denylist over
  it: no filesystem, shell, memory, configuration, credential, or database tool
  is reachable while a call is in progress — including tools provided by MCP
  servers.
- The system prompt limits disclosure to what the call's stated purpose
  requires and instructs the agent to refuse instructions originating from the
  callee. An automated red-team suite exercises instruction override,
  system-prompt exfiltration, tool enumeration, and social-engineered
  disclosure on every CI run.
- Transcripts and reports are masked before storage and before leaving the API:
  card numbers, national ID numbers, account numbers, PINs, and phone numbers.
- Logs are filtered so no full phone number reaches any log sink.

### F.4 Consent and AI disclosure

- Two-party consent announcement before the conversation begins
  (`PINCER_VOICE_CONSENT_MODE=two_party`); DE (+49) and CH (+41) numbers are
  treated as two-party automatically.
- The assistant identifies itself as an AI at the start of every call, whether
  or not recording is enabled (EU AI Act Art. 50).
- Announcements are rendered in the call language.

### F.5 Instance security

- No encryption at rest for SQLite in this release — see §G.
- Backups are encrypted (GPG symmetric) before leaving the host.
- Host baseline: TLS termination with automatic certificates, firewall
  allowlist, SSH keys only, unattended security upgrades, fail2ban.
- Dependency advisories block CI; updates are automated.

## G. Known limitations to disclose

State these plainly rather than letting a customer discover them:

1. **SQLite is not encrypted at rest.** Protection is filesystem permissions and
   full-disk encryption on the host. Encrypted-at-rest storage is on the
   backlog.
2. **Single-tenant only.** One instance serves one customer; there is no
   tenant isolation inside an instance.
3. **US sub-processors are on the default path.** An EU-only configuration is
   possible (Twilio IE1, an EU-region LLM provider, EU hosting) but reduces
   available features; it must be chosen deliberately.
4. **Speech may contain special-category data** even though none is requested.
   Retention limits and masking reduce but do not eliminate the exposure.
5. **`aiohttp` and `PyNaCl` carry known advisories** whose fixed versions are
   not yet resolvable through their upstream dependencies; tracked in
   `.security/pip-audit-ignore.txt` with an owner and a review date.

## H. Data-subject rights (Arts. 15–21)

| Right | How it is served |
|---|---|
| Access (Art. 15) | Transcripts and metadata are SQLite rows keyed by call SID and phone number; exportable with any SQLite client |
| Erasure (Art. 17) | `DELETE FROM call_transcripts WHERE call_id = …` and the corresponding `voice_calls` / `call_actions` rows; deletions land in the audit log |
| Objection (Art. 21) | `pincer voice dnc add <number>` — or the callee simply says so on the call, which adds them automatically |
| Rectification (Art. 16) | Transcripts are a record of what was said and are not rewritten; corrections are appended as call actions |

## I. Attachments to assemble before signing

- [ ] Twilio DPA (+ IE1 processing-region annex if used)
- [ ] Deepgram DPA — only if `media_streams`
- [ ] ElevenLabs DPA — if an ElevenLabs voice is configured
- [ ] LLM provider DPA
- [ ] Hosting provider AVV
- [ ] Backup storage AVV — if remote backups are enabled
- [ ] Transfer-impact assessment covering rows 1, 3, 4, 5 of §C
- [ ] The controller's Art. 30 record entry for voice calling
- [ ] The controller's privacy notice, reachable from the number being called

---

See also: [DACH Compliance (Voice)](dach-compliance.md) for the engineering
detail behind these statements, and
[Security Checklist](security-checklist.md) for the control-by-control review.
