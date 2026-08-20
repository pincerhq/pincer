# Pilot Expectations

Given to every pilot customer at handover, and signed off verbally in the
onboarding session. Its job is to make sure nothing in the pilot is a surprise —
including the parts that are not finished.

**Pilot:** `<customer>`  **Period:** `<start>` – `<end>`  **Owner:** `<name>`

---

## In scope

1. **Appointment scheduling by phone.** The best-tested path: check calendar →
   offer free slots → negotiate → book with invitations.
2. **General outbound calls.** Ask a question, confirm something, pass on a
   message. It reports what was said.
3. **Inbound calls** to your Pincer number — conversation, not booking.
4. Your existing chat channels (Telegram / web) as the way you ask.

## Explicitly out of scope for the pilot

These are not bugs. They are unbuilt, and a pilot that discovers them mid-call
has been mis-sold:

- Rescheduling or cancelling an appointment the agent booked.
- Inbound self-booking (your customer calls in and books themselves).
- Listening to a call live.
- Automatic detection of the caller's language.
- More than one business/location on one instance.

The full matrix, including caveats on what *is* built:
[voice-supported-features.md](../reference/voice-supported-features.md).

## Known limitations you will notice

| | |
|---|---|
| **Ukrainian** | Calls work; the written report comes back in English |
| **Cost figures** | Per-call costs shown are estimates until we set your real Twilio rates |
| **Availability figure** | Inferred from a health-check call every 6 hours, not continuously measured |
| **Call transfer / IVR menus** | Implemented but not yet proven by a real pilot — tell us if you hit either |

## Guardrails that will sometimes stop a call

Working as designed, not faults:

- **Quiet hours** (default 20:00–08:00) — no outbound calls.
- **Daily cap** (default 20 calls) — shared across everyone in your account.
- **Same-number cooldown** — at most 3 calls to one number per hour, retries included.
- **Do-not-call** — anyone who says "don't call me again" is blocked permanently
  and automatically, for your whole account.

All four are configuration. If a default does not fit your business, say so and
we will change it — deliberately, in writing.

## What we ask of you

| Cadence | What |
|---|---|
| Ongoing | Report anything odd, **with the call ID** from the report |
| Weekly, ~30 min | Feedback call: what worked, what did not, what you avoided using |
| Once, end of pilot | NPS, a testimonial (or a clear "no"), and honest churn risk |
| If you can | Help us ask 5–10 called parties two questions (below) |

### The callee questions

With your permission and introduction, we ask a sample of the people the agent
called:

1. *Did you realise you were speaking to an AI?* — checks the disclosure works.
2. *Was the call pleasant?* — our target is ≥ 80% yes.

## What you can expect from us

- **Bugs triaged within 24 hours** into: hotfix now / next sprint / won't fix for
  GA. You are told which, and why.
- **Weekly usage review** — call success, booking success, latency, cost.
- **No silent changes.** Anything that alters how your calls behave is told to
  you first.

## Kill switch

If the agent does something you want stopped immediately:

| | |
|---|---|
| Contact | `<name>` — `<phone>` / `<channel>`, `<hours>` |
| Self-service | Ask in chat to stop; outbound calling can be disabled instantly |
| Effect | New calls stop at once. A call already in progress ends politely, never mid-sentence silence |

## Data

Your calls produce transcripts stored on **your** instance, purged after 90 days.
Audio is never stored by us. Sub-processors and the technical measures are in
your AVV/DPA package ([annex](../guides/avv-annex.md)).

---

Acknowledged: `<customer name>` ______________________  Date: __________
