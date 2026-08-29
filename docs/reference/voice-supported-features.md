# Voice: Supported Features

What the voice surface does, and — with equal prominence — what it does not.
The "not yet" list is here because a pilot customer who discovers a gap during a
call has been mis-sold, and because a feature matrix that only lists wins is not
a feature matrix.

Verified against the codebase on **2026-08-20**. Anything marked ✅ has tests;
anything marked ⚠️ works but has a caveat you must read.

---

## Calling

| Feature | Status | Notes |
|---|---|---|
| Outbound calls (agent-initiated) | ✅ | Chat command, dashboard, or appointment scheduler |
| Inbound calls | ✅ | Caller allowlist via `PINCER_VOICE_ALLOWED_CALLERS` |
| ConversationRelay engine | ✅ | Production default; Twilio-hosted STT/TTS |
| Media Streams engine | ✅ | Deepgram STT + ElevenLabs TTS; needed for cloned voices |
| Barge-in (caller interrupts the agent) | ✅ | Native on ConversationRelay; turn-level cancellation on both |
| Answering-machine detection | ✅ | Voicemail is reported, never conversed with |
| Automatic redial after no-answer | ✅ | `PINCER_VOICE_RETRY_ATTEMPTS`; shares the abuse-gate budget |
| Call transfer to a human | ⚠️ | Implemented (`voice/transfer.py`), not exercised by a pilot yet |
| DTMF / IVR navigation | ⚠️ | `voice/ivr_navigator.py` exists; menu coverage is best-effort |
| **Live listen-in on an active call** | ⚠️ | Built (Sprint 15), **off by default**: `PINCER_LISTEN_IN_ENABLED=true`. Listen-only, announced on the call, audited, never recorded — see [DACH compliance](../guides/dach-compliance.md#live-listen-in-monitoring) |
| **Call recording playback** | ❌ | Recording is off by default and audio is never stored by us |

## Languages

| Feature | Status | Notes |
|---|---|---|
| English calls | ✅ | Full prompt pack, harness coverage |
| German calls (Sie/du) | ✅ | Full prompt pack, harness coverage, `PINCER_VOICE_DE_FORMALITY` |
| Ukrainian calls | ⚠️ | Prompt pack complete; post-call report falls back to English, date NLU is German-only |
| Mid-call language switch | ✅ | Only on explicit request; guarded by `[SWITCH_LANGUAGE:xx]` |
| **Automatic language detection at call start** | ❌ | Language is a parameter of the call, chosen when it is placed |
| Other languages | ❌ | Adding one is a prompt pack + a voice; see `voice/prompts/` |

## Appointment scheduling

| Feature | Status | Notes |
|---|---|---|
| Free/busy → candidate slots → negotiate → book | ✅ | Google Calendar; commits only inside the offered slots |
| Calendar invitations to attendees | ✅ | `sendUpdates=all` |
| Google Meet links | ✅ | `location_or_meet="meet"` |
| Idempotent calendar write | ✅ | Keyed by task id — a retry never double-books |
| Honest failure reporting | ✅ | A failed calendar write is never reported as booked |
| **Rescheduling an existing appointment** | ❌ | The agent can book, not move. Top of the post-GA backlog |
| **Cancellation calls** | ❌ | Same |
| **Inbound self-booking** (customer calls in to book) | ❌ | Inbound calls converse; they do not run the scheduling workflow |
| Multi-attendee coordination | ❌ | One callee per call |

## Compliance & safety

| Feature | Status | Notes |
|---|---|---|
| Two-party consent announcement | ✅ | Auto-applied for +49/+41 numbers |
| AI disclosure (EU AI Act Art. 50) | ✅ | Plays whether or not recording is on |
| Do-not-call list | ✅ | Auto-populated from the callee's own words (EN/DE/UK) |
| Quiet hours | ✅ | Default 20:00–08:00 local |
| Daily cap + per-target cooldown | ✅ | Enforced at one server-side gate for every channel |
| Transcript retention purge | ✅ | Default 90 days, audit-logged |
| PII masking (logs, transcripts, reports) | ✅ | Phone numbers, cards, national IDs, account numbers, PINs |
| Prompt-injection resistance | ⚠️ | Tool denylist is hard; prompt rules are probabilistic. See the security checklist |
| **Encrypted SQLite at rest** | ❌ | Filesystem permissions + host disk encryption only |

## Operations

| Feature | Status | Notes |
|---|---|---|
| Golden signals + alerting | ✅ | Works without a metrics backend |
| Synthetic canary | ✅ | Real loop call, default every 6h |
| Per-call cost record | ⚠️ | Complete, but only as accurate as your `PINCER_PRICE_*` rates |
| Weekly failure digest | ✅ | Week-over-week deltas |
| SLO tracking + error budget | ⚠️ | Availability is **inferred** from canary runs, not measured |
| Runbook | ✅ | 15 failure modes, every alert links to its section |
| **Multi-tenant (many customers per instance)** | ❌ | One instance per customer. See the capacity statement |
| **Postgres backend** | ❌ | SQLite only; the scale trigger is documented |

---

## The honest summary

If a pilot customer asks "can it…":

- **…book appointments by phone in German?** Yes, and that is the best-tested path.
- **…move or cancel an appointment it booked?** No. It books; a human moves.
- **…take a call from my customer and book them in?** It will have a conversation. It will not run the booking workflow.
- **…let me listen in live?** Yes, if the operator enables `PINCER_LISTEN_IN_ENABLED` — listen-only from the dashboard, the call announces that it may be monitored, every session is audited, nothing is recorded.
- **…work out which language the caller speaks?** No — you choose when the call is placed.
- **…handle two of my locations on one instance?** One instance per customer, and that is a deliberate boundary, not a bug.

See the [post-GA backlog](../operations/post-ga-backlog.md) for which of these
are queued and why they are ordered that way.
